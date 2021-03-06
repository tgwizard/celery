from __future__ import absolute_import

import errno
import os
import socket
import sys

from itertools import cycle

from celery.app.defaults import DEFAULTS
from celery.datastructures import AttributeDict
from celery.five import range
from celery.utils.functional import noop
from celery.utils.objects import Bunch

from celery.tests.case import AppCase, Mock, SkipTest, patch, restore_logging

try:
    from celery.concurrency import prefork as mp
    from celery.concurrency import asynpool
except ImportError:

    class _mp(object):
        RUN = 0x1

        class TaskPool(object):
            _pool = Mock()

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

            def stop(self):
                pass

            def apply_async(self, *args, **kwargs):
                pass
    mp = _mp()  # noqa
    asynpool = None  # noqa


class MockResult(object):

    def __init__(self, value, pid):
        self.value = value
        self.pid = pid

    def worker_pids(self):
        return [self.pid]

    def get(self):
        return self.value


class test_process_initializer(AppCase):

    @patch('celery.platforms.signals')
    @patch('celery.platforms.set_mp_process_title')
    def test_process_initializer(self, set_mp_process_title, _signals):
        with restore_logging():
            from celery import signals
            from celery._state import _tls
            from celery.concurrency.prefork import (
                process_initializer, WORKER_SIGRESET, WORKER_SIGIGNORE,
            )

            def on_worker_process_init(**kwargs):
                on_worker_process_init.called = True
            on_worker_process_init.called = False
            signals.worker_process_init.connect(on_worker_process_init)

            def Loader(*args, **kwargs):
                loader = Mock(*args, **kwargs)
                loader.conf = {}
                loader.override_backends = {}
                return loader

            with self.Celery(loader=Loader) as app:
                app.conf = AttributeDict(DEFAULTS)
                process_initializer(app, 'awesome.worker.com')
                _signals.ignore.assert_any_call(*WORKER_SIGIGNORE)
                _signals.reset.assert_any_call(*WORKER_SIGRESET)
                self.assertTrue(app.loader.init_worker.call_count)
                self.assertTrue(on_worker_process_init.called)
                self.assertIs(_tls.current_app, app)
                set_mp_process_title.assert_called_with(
                    'celeryd', hostname='awesome.worker.com',
                )

                with patch('celery.app.trace.setup_worker_optimizations') as S:
                    os.environ['FORKED_BY_MULTIPROCESSING'] = "1"
                    try:
                        process_initializer(app, 'luke.worker.com')
                        S.assert_called_with(app, 'luke.worker.com')
                    finally:
                        os.environ.pop('FORKED_BY_MULTIPROCESSING', None)

                os.environ['CELERY_LOG_FILE'] = 'worker%I.log'
                app.log.setup = Mock(name='log_setup')
                try:
                    process_initializer(app, 'luke.worker.com')
                finally:
                    os.environ.pop('CELERY_LOG_FILE', None)


class test_process_destructor(AppCase):

    @patch('celery.concurrency.prefork.signals')
    def test_process_destructor(self, signals):
        mp.process_destructor(13, -3)
        signals.worker_process_shutdown.send.assert_called_with(
            sender=None, pid=13, exitcode=-3,
        )


class MockPool(object):
    started = False
    closed = False
    joined = False
    terminated = False
    _state = None

    def __init__(self, *args, **kwargs):
        self.started = True
        self._timeout_handler = Mock()
        self._result_handler = Mock()
        self.maintain_pool = Mock()
        self._state = mp.RUN
        self._processes = kwargs.get('processes')
        self._pool = [Bunch(pid=i, inqW_fd=1, outqR_fd=2)
                      for i in range(self._processes)]
        self._current_proc = cycle(range(self._processes))

    def close(self):
        self.closed = True
        self._state = 'CLOSE'

    def join(self):
        self.joined = True

    def terminate(self):
        self.terminated = True

    def terminate_job(self, *args, **kwargs):
        pass

    def restart(self, *args, **kwargs):
        pass

    def handle_result_event(self, *args, **kwargs):
        pass

    def flush(self):
        pass

    def grow(self, n=1):
        self._processes += n

    def shrink(self, n=1):
        self._processes -= n

    def apply_async(self, *args, **kwargs):
        pass

    def register_with_event_loop(self, loop):
        pass


class ExeMockPool(MockPool):

    def apply_async(self, target, args=(), kwargs={}, callback=noop):
        from threading import Timer
        res = target(*args, **kwargs)
        Timer(0.1, callback, (res,)).start()
        return MockResult(res, next(self._current_proc))


class TaskPool(mp.TaskPool):
    Pool = BlockingPool = MockPool


class ExeMockTaskPool(mp.TaskPool):
    Pool = BlockingPool = ExeMockPool


class PoolCase(AppCase):

    def setup(self):
        try:
            import multiprocessing  # noqa
        except ImportError:
            raise SkipTest('multiprocessing not supported')


class test_AsynPool(PoolCase):

    def setup(self):
        if sys.platform == 'win32':
            raise SkipTest('win32: skip')

    def test_gen_not_started(self):

        def gen():
            yield 1
            yield 2
        g = gen()
        self.assertTrue(asynpool.gen_not_started(g))
        next(g)
        self.assertFalse(asynpool.gen_not_started(g))
        list(g)
        self.assertFalse(asynpool.gen_not_started(g))

    @patch('select.select', create=True)
    def test_select(self, __select):
        ebadf = socket.error()
        ebadf.errno = errno.EBADF
        with patch('select.poll', create=True) as poller:
            poll = poller.return_value = Mock(name='poll.poll')
            poll.return_value = {3}, set(), 0
            self.assertEqual(
                asynpool._select({3}, poll=poll),
                ({3}, set(), 0),
            )

            poll.return_value = {3}, set(), 0
            self.assertEqual(
                asynpool._select({3}, None, {3}, poll=poll),
                ({3}, set(), 0),
            )

            eintr = socket.error()
            eintr.errno = errno.EINTR
            poll.side_effect = eintr

            readers = {3}
            self.assertEqual(
                asynpool._select(readers, poll=poll),
                (set(), set(), 1),
            )
            self.assertIn(3, readers)

        with patch('select.poll') as poller:
            poll = poller.return_value = Mock(name='poll.poll')
            poll.side_effect = ebadf
            with patch('select.select') as selcheck:
                selcheck.side_effect = ebadf
                readers = {3}
                self.assertEqual(
                    asynpool._select(readers, poll=poll),
                    (set(), set(), 1),
                )
                self.assertNotIn(3, readers)

        with patch('select.poll') as poller:
            poll = poller.return_value = Mock(name='poll.poll')
            poll.side_effect = MemoryError()
            with self.assertRaises(MemoryError):
                asynpool._select({1}, poll=poll)

        with patch('select.poll') as poller:
            poll = poller.return_value = Mock(name='poll.poll')
            with patch('select.select') as selcheck:

                def se(*args):
                    selcheck.side_effect = MemoryError()
                    raise ebadf
                poll.side_effect = se
                with self.assertRaises(MemoryError):
                    asynpool._select({3}, poll=poll)

        with patch('select.poll') as poller:
            poll = poller.return_value = Mock(name='poll.poll')
            with patch('select.select') as selcheck:

                def se2(*args):
                    selcheck.side_effect = socket.error()
                    selcheck.side_effect.errno = 1321
                    raise ebadf
                poll.side_effect = se2
                with self.assertRaises(socket.error):
                    asynpool._select({3}, poll=poll)

        with patch('select.poll') as poller:
            poll = poller.return_value = Mock(name='poll.poll')

            poll.side_effect = socket.error()
            poll.side_effect.errno = 34134
            with self.assertRaises(socket.error):
                asynpool._select({3}, poll=poll)

    def test_promise(self):
        fun = Mock()
        x = asynpool.promise(fun, (1,), {'foo': 1})
        x()
        self.assertTrue(x.ready)
        fun.assert_called_with(1, foo=1)

    def test_Worker(self):
        w = asynpool.Worker(Mock(), Mock())
        w.on_loop_start(1234)
        w.outq.put.assert_called_with((asynpool.WORKER_UP, (1234,)))


class test_ResultHandler(PoolCase):

    def setup(self):
        if sys.platform == 'win32':
            raise SkipTest('win32: skip')

    def test_process_result(self):
        x = asynpool.ResultHandler(
            Mock(), Mock(), {}, Mock(),
            Mock(), Mock(), Mock(), Mock(),
            fileno_to_outq={},
            on_process_alive=Mock(),
            on_job_ready=Mock(),
        )
        self.assertTrue(x)
        hub = Mock(name='hub')
        recv = x._recv_message = Mock(name='recv_message')
        recv.return_value = iter([])
        x.on_state_change = Mock()
        x.register_with_event_loop(hub)
        proc = x.fileno_to_outq[3] = Mock()
        reader = proc.outq._reader
        reader.poll.return_value = False
        x.handle_event(6)  # KeyError
        x.handle_event(3)
        x._recv_message.assert_called_with(
            hub.add_reader, 3, x.on_state_change,
        )


class test_TaskPool(PoolCase):

    def test_start(self):
        pool = TaskPool(10)
        pool.start()
        self.assertTrue(pool._pool.started)
        self.assertTrue(pool._pool._state == asynpool.RUN)

        _pool = pool._pool
        pool.stop()
        self.assertTrue(_pool.closed)
        self.assertTrue(_pool.joined)
        pool.stop()

        pool.start()
        _pool = pool._pool
        pool.terminate()
        pool.terminate()
        self.assertTrue(_pool.terminated)

    def test_restart(self):
        pool = TaskPool(10)
        pool._pool = Mock(name='pool')
        pool.restart()
        pool._pool.restart.assert_called_with()
        pool._pool.apply_async.assert_called_with(mp.noop)

    def test_did_start_ok(self):
        pool = TaskPool(10)
        pool._pool = Mock(name='pool')
        self.assertIs(pool.did_start_ok(), pool._pool.did_start_ok())

    def test_register_with_event_loop(self):
        pool = TaskPool(10)
        pool._pool = Mock(name='pool')
        loop = Mock(name='loop')
        pool.register_with_event_loop(loop)
        pool._pool.register_with_event_loop.assert_called_with(loop)

    def test_on_close(self):
        pool = TaskPool(10)
        pool._pool = Mock(name='pool')
        pool._pool._state = mp.RUN
        pool.on_close()
        pool._pool.close.assert_called_with()

    def test_on_close__pool_not_running(self):
        pool = TaskPool(10)
        pool._pool = Mock(name='pool')
        pool._pool._state = mp.CLOSE
        pool.on_close()
        self.assertFalse(pool._pool.close.called)

    def test_apply_async(self):
        pool = TaskPool(10)
        pool.start()
        pool.apply_async(lambda x: x, (2,), {})

    def test_grow_shrink(self):
        pool = TaskPool(10)
        pool.start()
        self.assertEqual(pool._pool._processes, 10)
        pool.grow()
        self.assertEqual(pool._pool._processes, 11)
        pool.shrink(2)
        self.assertEqual(pool._pool._processes, 9)

    def test_info(self):
        pool = TaskPool(10)
        procs = [Bunch(pid=i) for i in range(pool.limit)]

        class _Pool(object):
            _pool = procs
            _maxtasksperchild = None
            timeout = 10
            soft_timeout = 5

            def human_write_stats(self, *args, **kwargs):
                return {}
        pool._pool = _Pool()
        info = pool.info
        self.assertEqual(info['max-concurrency'], pool.limit)
        self.assertEqual(info['max-tasks-per-child'], 'N/A')
        self.assertEqual(info['timeouts'], (5, 10))

    def test_num_processes(self):
        pool = TaskPool(7)
        pool.start()
        self.assertEqual(pool.num_processes, 7)
