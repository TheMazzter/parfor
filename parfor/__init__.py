from __future__ import print_function
import multiprocessing
import warnings
from tqdm.auto import tqdm
from dill import dumps, loads
from traceback import format_exc

def chunks(n, *args):
    """ Yield successive n-sized chunks from lists. """
    A = len(args)==1
    N = len(args[0])
    n = int(round(N/round(N/n)))
    for i in range(0, N, n):
        if A:
            yield args[0][i:i+n]
        else:
            yield [a[i:i+n] for a in args]

class tqdmm(tqdm):
    """ Overload tqdm to make a special version of tqdm functioning as a meter. """

    def __init__(self, *args, **kwargs):
        self._n = 0
        self.disable = False
        if not 'bar_format' in kwargs and len(args) < 16:
            kwargs['bar_format'] = '{n}/{total}'
        super(tqdmm, self).__init__(*args, **kwargs)

    @property
    def n(self):
        return self._n

    @n.setter
    def n(self, value):
        if not value == self.n:
            self._n = int(value)
            self.refresh()

    def __exit__(self, exc_type=None, exc_value=None, traceback=None):
        if not self.leave:
            self.n = self.total
        super(tqdmm, self).__exit__(exc_type, exc_value, traceback)

def parfor(*args, **kwargs):
    """ @parfor(iterator=None, args=(), kwargs={}, length=None, desc=None, bar=True, qbar=True, np1/3 serial=4, debug=False):
        decorator to parallize for-loops

        required arguments:
            fun:    function taking arguments: iteration from  iterable, other arguments defined in args & kwargs
            iterable: iterable from which an item is given to fun as a first argument

        optional arguments:
            args:   tuple with other unnamed arguments to fun
            kwargs: dict with other named arguments to fun
            length: give the length of the iterator in cases where len(iterator) results in an error
            desc:   string with description of the progress bar
            bar:    bool enable progress bar
            pbar:   bool enable buffer indicator bar
            nP:     number of workers, default: number of cpu's/3
            serial: switch to serial if number of tasks less than serial, default: 4
            debug:  if an error occurs in an iteration, return the erorr instead of retrying in the main process

        output:       list with results from applying the decorated function to each iteration of the iterator
                      specified as the first argument to the function

        examples:
            << from time import sleep

            <<
            @parfor(range(10), (3,))
            def fun(i, a):
                sleep(1)
                return a*i**2
            fun

            >> [0, 3, 12, 27, 48, 75, 108, 147, 192, 243]

            <<
            @parfor(range(10), (3,), bar=False)
            def fun(i, a):
                sleep(1)
                return a*i**2
            fun

            >> [0, 3, 12, 27, 48, 75, 108, 147, 192, 243]

            <<
            def fun(i, a):
                sleep(1)
                return a*i**2
            pmap(fun, range(10), (3,))

            >> [0, 3, 12, 27, 48, 75, 108, 147, 192, 243]

            equivalent to using the deco module:
            <<
            @concurrent
            def fun(i, a):
                time.sleep(1)
                return a*i**2

            @synchronized
            def run(iterator, a):
                res = []
                for i in iterator:
                    res.append(fun(i, a))
                return res

            run(range(10), 3)

            >> [0, 3, 12, 27, 48, 75, 108, 147, 192, 243]

            all equivalent to the serial for-loop:
            <<
            a = 3
            fun = []
            for i in range(10):
                sleep(1)
                fun.append(a*i**2)
            fun

            >> [0, 3, 12, 27, 48, 75, 108, 147, 192, 243]
        """
    def decfun(fun):
        return pmap(fun, *args, **kwargs)
    return decfun

class parpool(object):
    """ Parallel processing with addition of iterations at any time and request of that result any time after that.
        The target function and its argument can be changed at any time.
    """
    def __init__(self, fun=None, args=None, kwargs=None, nP=0.33, bar=None, qbar=None, debug=False):
        """ fun, args, kwargs: target function and its arguments and keyword arguments.
            nP: number of workers, or fraction of cpu cores to use, default: 0.33.
            bar, qbar: instances of tqdm and tqdmm to use for monitoring buffer and progress. """
        if nP is None:
            self.nP = int(multiprocessing.cpu_count()/3)
        elif nP<1:
            self.nP = int(nP*multiprocessing.cpu_count())
        else:
            self.nP = int(nP)
        self.fun = fun or (lambda x: x)
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.debug = debug
        if hasattr(multiprocessing, 'get_context'):
            ctx = multiprocessing.get_context('spawn')
        else:
            ctx = multiprocessing
        self.E = ctx.Event()
        self.Qi = ctx.Queue(3*self.nP)
        self.Qo = ctx.Queue(3*self.nP)
        self.P = ctx.Pool(self.nP, self._worker(self.Qi, self.Qo, self.E, self.debug))
        self.is_alive = True
        self.res = {}
        self.handle = 0
        self.handles = []
        self.bar = bar
        self.qbar = qbar
        if not self.qbar is None:
            self.qbar.total = 3*nP

    @property
    def fun(self):
        return self._fun[1:]

    @fun.setter
    def fun(self, fun):
        funs = dumps(fun, recurse=True)
        self._fun = (fun, hash(funs), funs)

    @property
    def args(self):
        return self._args[1:]

    @args.setter
    def args(self, args):
        argss = dumps(args, recurse=True)
        self._args = (args, hash(argss), argss)

    @property
    def kwargs(self):
        return self._kwargs[1:]

    @kwargs.setter
    def kwargs(self, kwargs):
        kwargss = dumps(kwargs, recurse=True)
        self._kwargs = (kwargs, hash(kwargss), kwargss)

    def __enter__(self, *args, **kwargs):
        return self

    def __exit__(self, *args, **kwargs):
        self.close()

    def _getfromq(self):
        """ Get an item from the queue and store it. """
        try:
            r = self.Qo.get(True, 0.02)
            if r[0]:
                pfmt = warnings.formatwarning
                warnings.formatwarning = lambda message, *args: '{}\n'.format(message)
                warnings.warn(
                    'Warning, error occurred in iteration {}. The iteration will be retried and should raise a '
                    'debuggable error. If it doesn\'t, it\'s an error specific to parallel execution.'
                        .format(r[1]))
                warnings.formatwarning = pfmt
                fun, args, kwargs = [loads(f[1]) for f in r[2][1:]]
                r = (False, r[1], fun(r[2][0], *args, **kwargs))
            self.res[r[1]] = r[2]
            if not self.bar is None:
                self.bar.update()
            self._qbar_update()
        except multiprocessing.queues.Empty:
            pass

    def __call__(self, n, fun=None, args=None, kwargs=None, handle=None):
        """ Add new iteration, using optional manually defined handle."""
        if not fun is None:
            self.fun = fun
        if not args is None:
            self.args = args
        if not kwargs is None:
            self.kwargs = kwargs
        while self.Qi.full():
            self._getfromq()
        if handle is None:
            handle = self.handle
            self.handle += 1
            self.handles.append(handle)
            self.Qi.put((handle, n, self.fun, self.args, self.kwargs))
            self._qbar_update()
            return handle
        elif not handle in self:
            self.handles.append(handle)
            self.Qi.put((handle, n, self.fun, self.args, self.kwargs))
        self._qbar_update()

    def _qbar_update(self):
        if not self.qbar is None:
            self.qbar.n = self.Qi.qsize()

    def __setitem__(self, handle, n):
        """ Add new iteration. """
        self(n, handle=handle)

    def __getitem__(self, handle):
        """ Request result and delete its record. Wait if result not yet available. """
        if not handle in self:
            raise ValueError('No handle: {}'.format(handle))
        while not handle in self.res:
            self._getfromq()
        self.handles.remove(handle)
        return self.res.pop(handle)

    def __delitem__(self, handle):
        self[handle]

    def __contains__(self, handle):
        return handle in self.handles

    def __repr__(self):
        if self.is_alive:
            return '{} with {} workers.'.format(self.__class__, self.nP)
        else:
            return 'Closed {}'.format(self.__class__)

    def close(self):
        if self.is_alive:
            self.E.set()
            self._close_queue(self.Qi)
            self._close_queue(self.Qo)
            self.P.close()
            self.P.join()
            self.is_alive = False
            self.res = {}
            self.handle = 0
            self.handles = []

    @staticmethod
    def _close_queue(Q):
        if not Q._closed:
            while Q.full():
                try:
                    Q.get(True, 0.02)
                except multiprocessing.queues.Empty:
                    pass
            Q.close()
        Q.join_thread()

    class _worker(object):
        """ Manages executing the target function which will be executed in different processes. """
        def __init__(self, Qi, Qo, E, debug=False, cachesize=48):
            self.cache = []
            self.Qi = Qi
            self.Qo = Qo
            self.E = E
            self.debug = debug
            self.cachesize = cachesize

        def __call__(self):
            while not self.E.is_set():
                i, n, Fun, Args, Kwargs = [None]*5
                try:
                    i, n, Fun, Args, Kwargs = self.Qi.get(True, 0.02)
                    fun = self.get_from_cache(*Fun)
                    args = self.get_from_cache(*Args)
                    kwargs = self.get_from_cache(*Kwargs)
                    self.Qo.put((False, i, fun(n, *args, **kwargs)))
                except multiprocessing.queues.Empty:
                    continue
                except:
                    if self.debug:
                        self.Qo.put((False, i, format_exc()))
                    else:
                        self.Qo.put((True, i, (n, Fun, Args, Kwargs)))

        def get_from_cache(self, h, ser):
            if len(self.cache):
                hs, objs = zip(*self.cache)
                if h in hs:
                    return objs[hs.index(h)]
            obj = loads(ser)
            self.cache.append((h, obj))
            while len(self.cache) > self.cachesize:
                self.cache.pop(0)
            return obj

def pmap(fun, iterable=None, args=None, kwargs=None, length=None, desc=None, bar=True, qbar=False, nP=0.33, serial=4, debug=False):
    """ map a function fun to each iteration in iterable
            best use: iterable is a generator and length is given to this function

        fun:    function taking arguments: iteration from  iterable, other arguments defined in args & kwargs
        iterable: iterable from which an item is given to fun as a first argument
        args:   tuple with other unnamed arguments to fun
        kwargs: dict with other named arguments to fun
        length: give the length of the iterator in cases where len(iterator) results in an error
        desc:   string with description of the progress bar
        bar:    bool enable progress bar
        pbar:   bool enable buffer indicator bar
        nP:     number of workers, default: number of cpu's/3
        serial: switch to serial if number of tasks less than serial, default: 4
        debug:  if an error occurs in an iteration, return the erorr instead of retrying in the main process
    """
    args = args or ()
    kwargs = kwargs or {}
    try:
        length = len(iterable)
    except:
        pass
    if length and length<serial: #serial case
        return [fun(c, *args, **kwargs) for c in iterable]
    else:                        #parallel case
        with tqdmm(total=0, desc='Task buffer', disable=not qbar, leave=False) as qbar,\
             tqdm(total=length, desc=desc, disable=not bar) as bar:
            with parpool(fun, args, kwargs, nP, bar, qbar, debug) as p:
                length = 0
                for i, j in enumerate(iterable): #add work to the queue
                    p[i] = j
                    if bar.total is None or bar.total < i+1:
                        bar.total = i+1
                    length += 1
                return [p[i] for i in range(length)] #collect the results