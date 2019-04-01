import weakref
from weakref import WeakValueDictionary

from collections import OrderedDict

from .scan import Scan, ScanBase

UNLOAD_POLICY_FULL = "unload_policy_full"
UNLOAD_POLICY_KEEP = "unload_policy_keep"


class ScanProxyContext(object):
    """A memory-conserving wrapper around an existing :class:`RandomAccessScanSource`
    object for serving :class:`ScanProxy` objects.

    Attributes
    ----------
    cache : :class:`collections.OrderedDict`
        A strong-reference maintaining cache from scan ID to :class:`ScanBase`
    cache_size : int
        The number of scans to keep strong references to.
    source : :class:`RandomAccessScanSource`
        The source to load scans from.
    """

    def __init__(self, source, cache_size=2 ** 10, unload_policy=None):
        if unload_policy is None:
            unload_policy = UNLOAD_POLICY_FULL
        self.source = source
        self.cache_size = cache_size
        self.cache = OrderedDict()
        self.unload_policy = unload_policy

    def get_scan_by_id(self, scan_id):
        '''Retrieve a real scan by its identifier.

        This method first checks the in-memory cache for the scan identifier
        and returns it if already loaded. If not, the scan is retrieved from
        :attr:`source`, usually from disk and saved to the cache using
        :meth:`_save_scan`. This may cause the cache eviction.

        Parameters
        ----------
        scan_id: :class:`str`
            The scan to retrieve

        Returns
        -------
        :class:`~.Scan`
        '''
        if scan_id in self.cache:
            return self.cache[scan_id]
        scan = self.source.get_scan_by_id(scan_id)
        self._save_scan(scan_id, scan)
        return scan

    def _save_scan(self, scan_id, scan):
        if len(self.cache) > self.cache_size:
            _, evicted_scan = self.cache.popitem()
            self.source._scan_cleared(evicted_scan)

        self.cache[scan_id] = scan

    def __call__(self, scan_id):
        """Create a proxy for the scan referenced by scan_id

        Parameters
        ----------
        scan_id: :class:`str`

        Returns
        -------
        :class:`ScanProxy`
        """
        return ScanProxy(scan_id, self)

    def clear(self):
        '''Clear the reference cache.

        If the scans that were previously held in the cache were not
        strong-referenced anywhere else, any extant proxies which depend
        upon this context will have to reload their spectra from disk.
        '''
        self.cache.clear()


class proxyproperty(object):
    '''An descriptor that integrates with :class:`ScanProxy` to retrieve attributes
    from a wrapped :class:`~.ScanBase` object referenced by :class:`ScanProxy`,
    potentially loading the scan from disk if it has been purged from the memory
    cache.

    Attributes
    ----------
    name: :class:`str`
        The name of the attribute to retrieve from the wrapped scan
    '''

    def __init__(self, name, caching=False):
        self.name = name
        self.caching = caching
        self.is_null_slot = "_%s_null" % self.name
        self.cache_slot = "_%s" % self.name

    def _refresh(self, instance):
        setattr(instance, self.is_null_slot, None)

    def _clear(self, instance):
        setattr(instance, self.is_null_slot, None)
        setattr(instance, self.cache_slot, None)

    def __get__(self, instance, owner):
        if self.caching:
            is_null_slot = self.is_null_slot
            cache_slot = self.cache_slot
            try:
                is_null = getattr(instance, is_null_slot)
            except AttributeError:
                setattr(instance, is_null_slot, None)
                is_null = None
            if is_null:
                return None
            try:
                value = getattr(instance, cache_slot)
                has_value = value is not None
            except AttributeError:
                has_value = False
            if not has_value:
                value = getattr(instance.scan, self.name)
                if is_null is None:
                    if value is None:
                        setattr(instance, is_null_slot, True)
                    else:
                        setattr(instance, is_null_slot, False)
                setattr(instance, cache_slot, value)
            return value
        return getattr(instance.scan, self.name)

    def __set__(self, instance, value):
        if self.caching:
            setattr(instance, self.cache_slot, value)
            setattr(instance, self.is_null_slot, None)
        else:
            raise TypeError("Cannot set attribute \"%s\"" % (self.name, ))

    def __delete__(self, instance):
        if self.caching:
            self._clear(instance)


class ScanProxy(ScanBase):
    """A proxy for a :class:`~.ScanBase` object, allowing transparent access to the
    scan, potentially loading it from disk through the attached :class:`ScanProxyContext`
    :attrib:`context`.

    Attributes
    ----------
    context : :class:`ScanProxyContext`
        The context to use to retrieve scans from, whose cache layer will keep the
        scan objects alive.
    scan : :class:`weakref.ProxyType`
        A weakref proxy to the :class:`~.ScanBase` object.
    """

    _names = [
        "arrays",
        "id",
        "title",
        "ms_level",
        "scan_time",
        "index",
        "is_profile",
        "polarity",
        "activation",
        "acquisition_information",
        "isolation_window",
        "instrument_configuration",
        "annotations",
    ]

    def __init__(self, scan_id, context):
        self._target_scan_id = scan_id
        self.context = context
        self._scan = None
        self._precursor_information = None
        self._peak_set = None

    @property
    def source(self):
        '''The source of the scan data this proxy is bound to.

        Returns
        -------
        :class:`RandomAccessScanSource`
        '''
        return self.context.source

    def _clear_scan(self, *args, **kwargs):
        self._scan = None
        if self.context.unload_policy == UNLOAD_POLICY_FULL:
            self._peak_set = None
            self._deconvoluted_peak_set = None

    def _require_scan(self):
        if self._scan is None:

            self._scan = weakref.proxy(
                self.context.get_scan_by_id(self._target_scan_id),
                self._clear_scan)

    @property
    def scan(self):
        '''The proxied scan.

        Accessing this property may require loading the scan's data
        from disk and/or triggering a cache eviction.
        '''
        self._require_scan()
        return self._scan

    def pick_peaks(self, *args, **kwargs):
        '''Request the proxied scan picked peaks if they are not
        already available and then cache them in memory.
        '''
        self._require_scan()
        peaks = self.peak_set
        if peaks is None:
            peaks = self.scan.pick_peaks(*args, **kwargs).peak_set
        self.peak_set = peaks
        return self

    def _resolve_sequence(self):
        deconvoluted_peak_set = self.deconvoluted_peak_set
        if deconvoluted_peak_set is not None:
            return deconvoluted_peak_set
        peak_set = self.peak_set
        if peak_set is not None:
            return peak_set
        self.pick_peaks()
        return self.peak_set

    def __getitem__(self, i):
        return self._resolve_sequence()[i]

    def __iter__(self):
        return iter(self._resolve_sequence())

    def __len__(self):
        return len(self._resolve_sequence())

    def has_peak(self, *args, **kwargs):
        '''Query the wrapped scan's peaks using it's :meth:`Scan.has_peak`
        method. If no peaks are available, this will call :meth:`pick_peaks`
        first to resolve the peak set and then query its peaks instead.

        Parameters
        ----------
        mz: float
            The m/z to search for
        error_tolerance: float
            The parts per million mass error tolerance to use

        Returns
        -------
        ms_peak_picker.FittedPeak or None
            The peak closest to the query m/z within the error tolerance window or None
            if there are no peaks satisfying the requirements

        See Also
        --------
        :meth:`Scan.has_peak`
        '''
        try:
            return self.scan.has_peak(*args, **kwargs)
        except ValueError:
            return self.pick_peaks().has_peak(*args, **kwargs)

    peak_set = proxyproperty('peak_set', True)
    deconvoluted_peak_set = proxyproperty('deconvoluted_peak_set', True)
    precursor_information = proxyproperty("precursor_information", True)

    def __repr__(self):
        template = "{self.__class__.__name__}({self._target_scan_id!r})"
        return template.format(self=self)

    @classmethod
    def _configure_proxy_attributes(cls):
        for name in cls._names:
            setattr(cls, name, proxyproperty(name))

    def _refresh(self):
        self.__class__.peak_set._refresh(self)
        self.__class__.deconvoluted_peak_set._refresh(self)
        self.__class__.precursor_information._refresh(self)


ScanProxy._configure_proxy_attributes()
