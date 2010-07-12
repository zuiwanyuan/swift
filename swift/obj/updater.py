# Copyright (c) 2010 OpenStack, LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import cPickle as pickle
import errno
import logging
import os
import signal
import sys
import time
from random import random

from eventlet import patcher, Timeout

from swift.common.bufferedhttp import http_connect
from swift.common.exceptions import ConnectionTimeout
from swift.common.ring import Ring
from swift.common.utils import get_logger, renamer
from swift.common.db_replicator import ReplConnection
from swift.obj.server import ASYNCDIR


class ObjectUpdater(object):
    """Update object information in container listings."""

    def __init__(self, server_conf, updater_conf):
        self.logger = get_logger(updater_conf, 'object-updater')
        self.devices = server_conf.get('devices', '/srv/node')
        self.mount_check = server_conf.get('mount_check', 'true').lower() in \
                              ('true', 't', '1', 'on', 'yes', 'y')
        swift_dir = server_conf.get('swift_dir', '/etc/swift')
        self.interval = int(updater_conf.get('interval', 300))
        self.container_ring_path = os.path.join(swift_dir, 'container.ring.gz')
        self.container_ring = None
        self.concurrency = int(updater_conf.get('concurrency', 1))
        self.slowdown = float(updater_conf.get('slowdown', 0.01))
        self.node_timeout = int(updater_conf.get('node_timeout', 10))
        self.conn_timeout = float(updater_conf.get('conn_timeout', 0.5))
        self.successes = 0
        self.failures = 0

    def get_container_ring(self):
        """Get the container ring.  Load it, if it hasn't been yet."""
        if not self.container_ring:
            self.logger.debug(
                'Loading container ring from %s' % self.container_ring_path)
            self.container_ring = Ring(self.container_ring_path)
        return self.container_ring

    def update_forever(self):   # pragma: no cover
        """Run the updater continuously."""
        time.sleep(random() * self.interval)
        while True:
            self.logger.info('Begin object update sweep')
            begin = time.time()
            pids = []
            # read from container ring to ensure it's fresh
            self.get_container_ring().get_nodes('')
            for device in os.listdir(self.devices):
                if self.mount_check and not \
                        os.path.ismount(os.path.join(self.devices, device)):
                    self.logger.warn(
                        'Skipping %s as it is not mounted' % device)
                    continue
                while len(pids) >= self.concurrency:
                    pids.remove(os.wait()[0])
                pid = os.fork()
                if pid:
                    pids.append(pid)
                else:
                    signal.signal(signal.SIGTERM, signal.SIG_DFL)
                    patcher.monkey_patch(all=False, socket=True)
                    self.successes = 0
                    self.failures = 0
                    forkbegin = time.time()
                    self.object_sweep(os.path.join(self.devices, device))
                    elapsed = time.time() - forkbegin
                    self.logger.info('Object update sweep of %s completed: '
                        '%.02fs, %s successes, %s failures' %
                        (device, elapsed, self.successes, self.failures))
                    sys.exit()
            while pids:
                pids.remove(os.wait()[0])
            elapsed = time.time() - begin
            self.logger.info('Object update sweep completed: %.02fs' % elapsed)
            if elapsed < self.interval:
                time.sleep(self.interval - elapsed)

    def update_once_single_threaded(self):
        """Run the updater once"""
        self.logger.info('Begin object update single threaded sweep')
        begin = time.time()
        self.successes = 0
        self.failures = 0
        for device in os.listdir(self.devices):
            if self.mount_check and \
                    not os.path.ismount(os.path.join(self.devices, device)):
                self.logger.warn(
                    'Skipping %s as it is not mounted' % device)
                continue
            self.object_sweep(os.path.join(self.devices, device))
        elapsed = time.time() - begin
        self.logger.info('Object update single threaded sweep completed: '
            '%.02fs, %s successes, %s failures' %
            (elapsed, self.successes, self.failures))

    def object_sweep(self, device):
        """
        If there are async pendings on the device, walk each one and update.

        :param device: path to device
        """
        async_pending = os.path.join(device, ASYNCDIR)
        if not os.path.isdir(async_pending):
            return
        for prefix in os.listdir(async_pending):
            prefix_path = os.path.join(async_pending, prefix)
            if not os.path.isdir(prefix_path):
                continue
            for update in os.listdir(prefix_path):
                update_path = os.path.join(prefix_path, update)
                if not os.path.isfile(update_path):
                    continue
                self.process_object_update(update_path, device)
                time.sleep(self.slowdown)
            try:
                os.rmdir(prefix_path)
            except OSError:
                pass

    def process_object_update(self, update_path, device):
        """
        Process the object information to be updated and update.

        :param update_path: path to pickled object update file
        :param device: path to device
        """
        try:
            update = pickle.load(open(update_path, 'rb'))
        except Exception, err:
            self.logger.exception(
                'ERROR Pickle problem, quarantining %s' % update_path)
            renamer(update_path, os.path.join(device,
                'quarantined', 'objects', os.path.basename(update_path)))
            return
        part, nodes = self.get_container_ring().get_nodes(
                                update['account'], update['container'])
        obj = '/%s/%s/%s' % \
              (update['account'], update['container'], update['obj'])
        success = True
        for node in nodes:
            status = self.object_update(node, part, update['op'], obj,
                                        update['headers'])
            if not (200 <= status < 300) and status != 404:
                success = False
        if success:
            self.successes += 1
            self.logger.debug('Update sent for %s %s' % (obj, update_path))
            os.unlink(update_path)
        else:
            self.failures += 1
            self.logger.debug('Update failed for %s %s' % (obj, update_path))

    def object_update(self, node, part, op, obj, headers):
        """
        Perform the object update to the container

        :param node: node dictionary from the container ring
        :param part: partition that holds the container
        :param op: operation performed (ex: 'POST' or 'DELETE')
        :param obj: object name being updated
        :param headers: headers to send with the update
        """
        try:
            with ConnectionTimeout(self.conn_timeout):
                conn = http_connect(node['ip'], node['port'], node['device'],
                    part, op, obj, headers)
            with Timeout(self.node_timeout):
                resp = conn.getresponse()
                resp.read()
                return resp.status
        except:
            self.logger.exception('ERROR with remote server '
                '%(ip)s:%(port)s/%(device)s' % node)
        return 500