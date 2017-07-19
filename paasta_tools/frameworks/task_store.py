from __future__ import absolute_import
from __future__ import unicode_literals

import json

from kazoo.client import KazooClient
from kazoo.exceptions import NoNodeError

from paasta_tools.utils import _log


class MesosTaskParametersIsImmutableError(Exception):
    pass


class MesosTaskParameters(object):
    def __init__(
        self,
        health=None,
        mesos_task_state=None,
        is_draining=None,
        is_healthy=None,
        offer=None,
        resources=None,
    ):
        self.__dict__['health'] = health
        self.__dict__['mesos_task_state'] = mesos_task_state
        self.__dict__['is_draining'] = is_draining
        self.__dict__['is_healthy'] = is_healthy
        self.__dict__['offer'] = offer
        self.__dict__['resources'] = resources

    def __eq__(self, other):
        return self.__dict__ == other.__dict__

    def __repr__(self):
        return "%s(\n    %s)" % (type(self).__name__, ',\n    '.join(["%s=%r" % kv for kv in self.__dict__.items()]))

    def __setattr__(self, name, value):
        raise MesosTaskParametersIsImmutableError()

    def __delattr__(self, name):
        raise MesosTaskParametersIsImmutableError()

    def merge(self, **kwargs):
        """Return a merged MesosTaskParameters object, where attributes in other take precedence over self."""

        new_dict = self.__dict__
        new_dict.update(kwargs)

        return MesosTaskParameters(**new_dict)

    @classmethod
    def deserialize(cls, serialized_params):
        return cls(**json.loads(serialized_params))

    def serialize(self):
        return json.dumps(self.__dict__).encode('utf-8')


class TaskStore(object):
    def __init__(self, service_name, instance_name, framework_id, system_paasta_config):
        self.service_name = service_name
        self.instance_name = instance_name
        self.framework_id = framework_id
        self.system_paasta_config = system_paasta_config

    def get_task(self, task_id):
        """Get task data for task_id. If we don't know about task_id, return None"""
        raise NotImplementedError()

    def get_all_tasks(self):
        """Returns a dictionary of task_id -> MesosTaskParameters for all known tasks."""
        raise NotImplementedError()

    def overwrite_task(self, task_id, params):
        raise NotImplementedError()

    def add_task_if_doesnt_exist(self, task_id, **kwargs):
        """Add a task if it does not already exist. If it already exists, do nothing."""
        if self.get_task(task_id) is not None:
            return
        else:
            self.overwrite_task(task_id, MesosTaskParameters(**kwargs))

    def update_task(self, task_id, **kwargs):
        existing_task = self.get_task(task_id)
        if existing_task:
            merged_params = existing_task.merge(**kwargs)
        else:
            merged_params = MesosTaskParameters(**kwargs)

        self.overwrite_task(task_id, merged_params)
        return merged_params

    def garbage_collect_old_tasks(self, max_dead_task_age):
        # TODO: call me.
        # TODO: implement in base class.
        raise NotImplementedError()


class DictTaskStore(TaskStore):
    def __init__(self, service_name, instance_name, framework_id, system_paasta_config):
        self.tasks = {}
        super(DictTaskStore, self).__init__(service_name, instance_name, framework_id, system_paasta_config)

    def get_task(self, task_id):
        return self.tasks.get(task_id)

    def get_all_tasks(self):
        """Returns a dictionary of task_id -> MesosTaskParameters for all known tasks."""
        return dict(self.tasks)

    def overwrite_task(self, task_id, params):
        # serialize/deserialize to make sure the returned values are the same format as ZKTaskStore.
        self.tasks[task_id] = MesosTaskParameters.deserialize(params.serialize())


class ZKTaskStore(TaskStore):
    def __init__(self, service_name, instance_name, framework_id, system_paasta_config):
        super(ZKTaskStore, self).__init__(service_name, instance_name, framework_id, system_paasta_config)
        self.zk_hosts = system_paasta_config.get_zk_hosts()

        # For some reason, I could not get the code suggested by this SO post to work to ensure_path on the chroot.
        # https://stackoverflow.com/a/32785625/25327
        # Plus, it just felt dirty to modify instance attributes of a running connection, especially given that
        # KazooClient.set_hosts() doesn't allow you to change the chroot. Must be for a good reason.

        chroot = 'task_store/%s/%s/%s' % (service_name, instance_name, framework_id)

        temp_zk_client = KazooClient(hosts=self.zk_hosts)
        temp_zk_client.start()
        temp_zk_client.ensure_path(chroot)
        temp_zk_client.stop()
        temp_zk_client.close()

        self.zk_client = KazooClient(hosts='%s/%s' % (self.zk_hosts, chroot))
        self.zk_client.start()
        self.zk_client.ensure_path('/')
        # TODO: call self.zk_client.stop() and .close()

    def get_task(self, task_id):
        try:
            data, stat = self.zk_client.get('/%s' % task_id)
            return MesosTaskParameters.deserialize(data)
        except NoNodeError:
            return None
        except json.decoder.JSONDecodeError:
            _log(
                service=self.service_name,
                instance=self.instance_name,
                level='debug',
                component='deploy',
                line='Warning: found non-json-decodable value in zookeeper for task %s: %s' % (task_id, data),
            )
            return None

    def get_all_tasks(self):
        all_tasks = {}

        for child_path in self.zk_client.get_children('/'):
            task_id = self._task_id_from_zk_path(child_path)
            params = self.get_task(task_id)
            # sometimes there are bogus child ZK nodes. Ignore them.
            if params is not None:
                all_tasks[task_id] = params

        return all_tasks

    def overwrite_task(self, task_id, params):
        try:
            self.zk_client.set(self._zk_path_from_task_id(task_id), params.serialize())
        except NoNodeError:
            self.zk_client.create(self._zk_path_from_task_id(task_id), params.serialize())

    def _zk_path_from_task_id(self, task_id):
        return '/%s' % task_id

    def _task_id_from_zk_path(self, zk_path):
        return zk_path.lstrip('/')
