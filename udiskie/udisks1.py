"""
UDisks wrapper utilities.

These act as a convenience abstraction layer on the UDisks DBus service.
Requires UDisks 1.0.5 as described here:

    http://udisks.freedesktop.org/docs/1.0.5/

This wraps the DBus API of Udisks2 providing a common interface with the
udisks2 module.

Overview: This module exports the classes ``Sniffer`` and ``Daemon``.

:class:`Sniffer` can be used as an online exporter of the current device
states queried from the UDisks DBus service as requested.

:class:`Daemon` caches all device states and listens to UDisks events to
guarantee the accessibilityy of device properties in between operations.
"""

from copy import copy
from inspect import getmembers
import logging
import os.path

from udiskie.common import Emitter, samefile
from udiskie.compat import filter
from udiskie.dbus import DBusProxy, DBusService


__all__ = ['Sniffer', 'Daemon']


def filter_opt(opt):
    """Remove ``None`` values from a dictionary."""
    return [k for k,v in opt.items() if v is not None]


class DeviceBase(object):

    """Helper base class for devices."""

    Interface = 'org.freedesktop.UDisks.Device'

    # string representation
    def __str__(self):
        """Display as object path."""
        return self.object_path

    def __eq__(self, other):
        """Comparison by object path."""
        return self.object_path == str(other)

    def __ne__(self, other):
        """Comparison by object path."""
        return not (self == other)

    def __nonzero__(self):      # python2
        """Check device validity."""
        return self.is_valid
    __bool__ = __nonzero__      # python3

    def is_file(self, path):
        """Comparison by mount and device file path."""
        return samefile(path, self.device_file) or any(
            samefile(path, mp) for mp in self.mount_paths)


class OnlineDevice(DBusProxy, DeviceBase):

    """
    Online wrapper for org.freedesktop.UDisks.Device DBus API proxy objects.

    Resolves both property access and method calls dynamically to the DBus
    object.

    This is the main class used to retrieve (and then possibly cache) device
    properties from the DBus backend.
    """

    # construction
    def __init__(self, udisks, proxy):
        """
        Initialize an instance with the given DBus proxy object.

        proxy must be an object acquired by a call to bus.get_object().
        """
        super(OnlineDevice, self).__init__(proxy, self.Interface)
        self.udisks = udisks

    # availability of interfaces
    @property
    def is_valid(self):
        """Check if there is a valid DBus object for this object path."""
        try:
            self.property.DeviceFile
            return True
        except self.Exception:
            return False

    @property
    def is_drive(self):
        """Check if the device is a drive."""
        return self.property.DeviceIsDrive

    @property
    def is_block(self):
        """Check if the device is a block device."""
        return True

    @property
    def is_partition_table(self):
        """Check if the device is a partition table."""
        return self.property.DeviceIsPartitionTable

    @property
    def is_partition(self):
        """Check if the device has a partition slave."""
        return self.property.DeviceIsPartition

    @property
    def is_filesystem(self):
        """Check if the device is a filesystem."""
        return self.id_usage == 'filesystem'

    @property
    def is_luks(self):
        """Check if the device is a LUKS container."""
        return self.property.DeviceIsLuks

    #----------------------------------------
    # Drive
    #----------------------------------------

    # Drive properties
    is_toplevel = is_drive

    @property
    def is_detachable(self):
        """Check if the drive that owns this device can be detached."""
        return self.property.DriveCanDetach if self.is_drive else None

    @property
    def is_ejectable(self):
        """Check if the drive that owns this device can be ejected."""
        return self.property.DriveIsMediaEjectable if self.is_drive else None

    @property
    def has_media(self):
        """Check if there is media available in the drive."""
        return self.property.DeviceIsMediaAvailable

    # Drive methods
    def eject(self, unmount=None):
        """Eject media from the device."""
        return self.method.DriveEject(filter_opt({'unmount': unmount}))

    def detach(self):
        """Detach the device by e.g. powering down the physical port."""
        return self.method.DriveDetach([])

    #----------------------------------------
    # Block
    #----------------------------------------

    # Block properties
    @property
    def device_file(self):
        """The filesystem path of the device block file."""
        return os.path.normpath(self.property.DeviceFile)

    @property
    def device_presentation(self):
        """The device file path to present to the user."""
        return self.property.DeviceFilePresentation

    # TODO: device_size missing

    @property
    def id_usage(self):
        """Device usage class, for example 'filesystem' or 'crypto'."""
        return self.property.IdUsage

    @property
    def is_crypto(self):
        """Check if the device is a crypto device."""
        return self.id_usage == 'crypto'

    @property
    def is_ignored(self):
        """Check if the device should be ignored."""
        return self.property.DevicePresentationHide

    @property
    def id_type(self):
        """"
        Return IdType property.

        This field provides further detail on IdUsage, for example:

        IdUsage     'filesystem'    'crypto'
        IdType      'ext4'          'crypto_LUKS'
        """
        return self.property.IdType

    @property
    def id_label(self):
        """Label of the device if available."""
        return self.property.IdLabel

    @property
    def id_uuid(self):
        """Device UUID."""
        return self.property.IdUuid

    @property
    def luks_cleartext_slave(self):
        """Get luks crypto device."""
        return self.udisks[self.property.LuksCleartextSlave] if self.is_luks_cleartext else None

    @property
    def is_luks_cleartext(self):
        """Check whether this is a luks cleartext device."""
        return self.property.DeviceIsLuksCleartext

    @property
    def is_external(self):
        """Check if the device is external."""
        return not self.is_systeminternal

    @property
    def is_systeminternal(self):
        """Check if the device is internal."""
        return self.property.DeviceIsSystemInternal

    @property
    def drive(self):
        """
        Get the drive containing this device.

        The returned Device object is not guaranteed to be a drive.
        """
        if self.is_partition:
            return self.partition_slave.drive
        elif self.is_luks_cleartext:
            return self.luks_cleartext_slave.drive
        else:
            return self

    root = drive

    #----------------------------------------
    # Partition
    #----------------------------------------

    # Partition properties
    @property
    def partition_slave(self):
        """Get the partition slave (container)."""
        return self.udisks[self.property.PartitionSlave] if self.is_partition else None

    #----------------------------------------
    # Filesystem
    #----------------------------------------

    # Filesystem properties
    @property
    def is_mounted(self):
        """Check if the device is mounted."""
        return self.property.DeviceIsMounted

    @property
    def mount_paths(self):
        """Return list of active mount paths."""
        if not self.is_mounted:
            return []
        raw_paths = self.property.DeviceMountPaths
        return [os.path.normpath(path) for path in raw_paths]

    # Filesystem methods
    def mount(self,
              fstype=None,
              options=None,
              auth_no_user_interaction=None):
        """Mount filesystem."""
        options = list(filter(None, (options or '').split(','))) + filter_opt({
            'auth_no_user_interaction': auth_no_user_interaction
        })
        return self.method.FilesystemMount(fstype or self.id_type, options)

    def unmount(self, force=None):
        """Unmount filesystem."""
        return self.method.FilesystemUnmount(filter_opt({'force': force}))

    #----------------------------------------
    # Encrypted
    #----------------------------------------

    # Encrypted properties
    @property
    def luks_cleartext_holder(self):
        """Get unlocked luks cleartext device."""
        return self.udisks[self.property.LuksHolder] if self.is_luks else None

    @property
    def is_unlocked(self):
        """Check if device is already unlocked."""
        return self.luks_cleartext_holder if self.is_luks else None

    # Encrypted methods
    def unlock(self, password):
        """Unlock Luks device."""
        return self.udisks.update(self.method.LuksUnlock(password, []))

    def lock(self):
        """Lock Luks device."""
        return self.method.LuksLock([])

    #----------------------------------------
    # derived properties
    #----------------------------------------

    @property
    def in_use(self):
        """Check whether this device is in use, i.e. mounted or unlocked."""
        if self.is_mounted or self.is_unlocked:
            return True
        if self.is_partition_table:
            for device in self.udisks:
                if device.partition_slave == self and device.in_use:
                    return True
        return False


def _CachedDeviceProperty(method):
    """Cache object path and return the current known CachedDevice state."""
    key = '_'+method.__name__
    def get(self):
        return self._daemon[getattr(self, key, None)]
    def set(self, device):
        setattr(self, key, getattr(device, 'object_path', None))
    return property(get, set, doc=method.__doc__)


class CachedDevice(DeviceBase):

    """
    Cached device state.

    Properties are cached at creation time. Methods will be invoked
    dynamically via the associated DBus object.
    """

    def __init__(self, device):
        """Cache all properties of the online device."""
        self._device = device
        self._daemon = device.udisks
        def isproperty(obj):
            return isinstance(obj, property)
        for key,val in getmembers(device.__class__, isproperty):
            try:
                setattr(self, key, getattr(device, key))
            except device.Exception:
                setattr(self, key, None)
        self.is_valid = device.is_valid

    def __getattr__(self, key):
        """Resolve unknown properties and methods via the online device."""
        return getattr(self._device, key)

    # Overload properties that return Device objects to return CachedDevice
    # instances instead. NOTE: the setters are implemented such that the
    # returned devices will be cached at the time the property is accessed
    # rather than at the time the current object was instanciated.
    # FIXME: should it be different?

    @_CachedDeviceProperty
    def luks_cleartext_slave(self):
        """Get luks crypto device."""
        pass

    @_CachedDeviceProperty
    def drive(self):
        """Get the drive."""
        pass

    @_CachedDeviceProperty
    def partition_slave(self):
        """Get the partition slave (container)."""
        pass

    @_CachedDeviceProperty
    def luks_cleartext_holder(self):
        """Get unlocked luks cleartext device."""
        pass

    def unlock(self, password, options=[]):
        """Unlock Luks device."""
        return CachedDevice(self._device.unlock(password))


class UDisks(DBusService):

    """
    Base class for UDisks service wrappers.
    """

    BusName = 'org.freedesktop.UDisks'
    Interface = 'org.freedesktop.UDisks'
    ObjectPath = '/org/freedesktop/UDisks'

    def __iter__(self):
        """Iterate over all devices."""
        return filter(None, map(self.get, self.paths()))

    def __getitem__(self, object_path):
        return self.get(object_path)

    def find(self, path):
        """
        Get a device proxy by device name or any mount path of the device.

        This searches through all accessible devices and compares device
        path as well as mount pathes.
        """
        for device in self:
            if device.is_file(path):
                return device
        logger = logging.getLogger(__name__)
        logger.warn('Device not found: %s' % path)
        return None


class Sniffer(UDisks):

    """
    UDisks DBus service wrapper.

    This is a wrapper for the DBus API of the UDisks service at
    'org.freedesktop.UDisks'. Access to properties and device states is
    completely online, meaning the properties are requested from dbus as
    they are accessed in the python object.
    """

    # Construction
    def __init__(self, proxy=None):
        """
        Initialize an instance with the given DBus proxy object.

        :param common.DBusProxy proxy: proxy to udisks object
        """
        self._proxy = proxy or self.connect_service()

    def paths(self):
        return self._proxy.method.EnumerateDevices()

    def get(self, object_path):
        """Create a Device instance from object path."""
        return OnlineDevice(self, self._proxy._bus.get_object(self.BusName,
                                                              object_path))
    update = get


class Job(object):

    """Job information struct for devices."""

    def __init__(self, job_id, percentage):
        self.job_id = job_id
        self.percentage = percentage


class Daemon(Emitter, UDisks):

    """
    UDisks listener daemon.

    Listens to UDisks events. When a change occurs this class detects what
    has changed and triggers an appropriate event. Valid events are:

        - device_added    / device_removed
        - device_unlocked / device_locked
        - device_mounted  / device_unmounted
        - media_added     / media_removed
        - device_changed  / job_failed

    A very primitive mechanism that gets along without external
    dependencies is used for event dispatching. The methods `connect` and
    `disconnect` can be used to add or remove event handlers.
    """

    mainloop = True

    def __init__(self, proxy=None):
        """
        Create a Daemon object and start listening to DBus events.

        :param common.DBusProxy proxy: proxy to the dbus service object
        :param udisks1.Sniffer sniffer: sniffer to use

        If neither proxy nor sniffer are given they will be created and
        dbus will be configured for the gobject mainloop.
        """
        event_names = [stem + suffix
                       for suffix in ('ed', 'ing')
                       for stem in (
                           'device_add',
                           'device_remov',
                           'device_mount',
                           'device_unmount',
                           'media_add',
                           'media_remov',
                           'device_unlock',
                           'device_lock',
                           'device_chang', )] + ['job_failed']
        super(Daemon, self).__init__(event_names)

        sniffer = Sniffer(proxy or self.connect_service())

        self._sniffer = sniffer
        self._jobs = {}
        self._devices = {}
        self._errors = {'mount': {}, 'unmount': {},
                        'unlock': {}, 'lock': {},
                        'eject': {}, 'detach': {}}

        self.connect('device_changed', self._on_device_changed)
        bus = self._sniffer._proxy._bus
        bus.add_signal_receiver(
            self._device_added,
            signal_name='DeviceAdded',
            bus_name=self.BusName)
        bus.add_signal_receiver(
            self._device_removed,
            signal_name='DeviceRemoved',
            bus_name=self.BusName)
        bus.add_signal_receiver(
            self._device_changed,
            signal_name='DeviceChanged',
            bus_name=self.BusName)
        bus.add_signal_receiver(
            self._device_job_changed,
            signal_name='DeviceJobChanged',
            bus_name=self.BusName)
        self._sync()

    # Sniffer overrides
    def paths(self):
        """Iterate over all valid cached devices."""
        return (object_path
                for object_path,device in self._devices.items()
                if device)

    def get(self, object_path):
        """Return the current cached state of the device."""
        return self._devices.get(object_path)

    def update(self, object_path):
        device = self._sniffer.get(object_path)
        cached = CachedDevice(device)
        if cached or object_path not in self._devices:
            self._devices[object_path] = cached
        else:
            self._invalidate(object_path)
        return cached

    # special methods
    def set_error(self, device, action, message):
        self._errors[action][device.object_path] = message

    # events
    def _on_device_changed(self, old_state, new_state):
        """Detect type of event and trigger appropriate event handlers."""
        d = {}
        d['media_added'] = new_state.has_media and not old_state.has_media
        d['media_removed'] = old_state.has_media and not new_state.has_media
        for event in d:
            if d[event]:
                self.trigger(event, new_state)

    # UDisks event listeners
    def _device_added(self, object_path):
        """Internal method."""
        new_state = self.update(object_path)
        self.trigger('device_added', new_state)

    def _device_removed(self, object_path):
        """Internal method."""
        old_state = self[object_path]
        self._invalidate(object_path)
        self.trigger('device_removed', old_state)

    def _device_changed(self, object_path):
        """Internal method."""
        old_state = self[object_path]
        new_state = self.update(object_path)
        self.trigger('device_changed', old_state, new_state)

    # NOTE: it seems the UDisks1 documentation for DeviceJobChanged is
    # fatally incorrect!
    def _device_job_changed(self,
                            object_path,
                            job_in_progress,
                            job_id,
                            job_initiated_by_user,
                            job_is_cancellable,
                            job_percentage):
        """
        Detect type of event and trigger appropriate event handlers.

        Internal method.
        """
        if not job_in_progress and object_path in self._jobs:
            job_id = self._jobs[object_path].job_id
        try:
            action = self._action_mapping[job_id]
        except KeyError:
            return
        event_name = self._event_mapping[action]
        dev = self[object_path]
        # NOTE: The here used heuristic is prone to raise conditions.
        if job_in_progress:
            self.trigger(event_name + 'ing', dev, job_percentage)
            self._jobs[object_path] = Job(job_id, job_percentage)
        elif self._check_success[job_id](dev):
            self.trigger(event_name + 'ed', dev)
            del self._jobs[object_path]
        else:
            # get and delete message, if available:
            message = self._errors[action].pop(object_path, "")
            self.trigger('job_failed', dev, 'device_' + action, message)
            log = logging.getLogger(__name__)
            log.info('%s operation failed for device: %s' % (job_id, object_path))

    # used internally by _device_job_changed:
    _action_mapping = {
        'FilesystemMount': 'mount',
        'FilesystemUnmount': 'unmount',
        'LuksUnlock': 'unlock',
        'LuksLock': 'lock',
        'DriveDetach': 'detach',
        'DriveEject': 'eject' }

    _event_mapping = {'mount': 'device_mount',
                      'unmount': 'device_unmount',
                      'unlock': 'device_unlock',
                      'lock': 'device_lock',
                      'eject': 'media_remov',
                      'detach': 'device_remov'}

    _check_success = {
        'FilesystemMount': lambda dev: dev.is_mounted,
        'FilesystemUnmount': lambda dev: not dev or not dev.is_mounted,
        'LuksUnlock': lambda dev: dev.is_unlocked,
        'LuksLock': lambda dev: not dev or not dev.is_unlocked,
        'DriveDetach': lambda dev: not dev,
        'DriveEject': lambda dev: not dev or not dev.has_media
    }

    # internal state keeping
    def _sync(self):
        """Cache all device states."""
        self._devices = { dev.object_path: dev for dev in self._sniffer }
        self._devices = {
            object_path: CachedDevice(device)
            for object_path,device in self._devices.items() }

    def _invalidate(self, object_path):
        """Flag the device invalid. This removes it from the iteration."""
        if object_path in self._devices:
            update = copy(self._devices[object_path])
            update.is_valid = False
            self._devices[object_path] = update
