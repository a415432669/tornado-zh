#!/usr/bin/env python
# coding: utf-8
#
# Copyright 2015 The Tornado Authors
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from __future__ import absolute_import, division, print_function, with_statement

__all__ = ['Condition', 'Event', 'Semaphore', 'BoundedSemaphore', 'Lock']

import collections

from tornado import gen, ioloop
from tornado.concurrent import Future


class _TimeoutGarbageCollector(object):
    """Base class for objects that periodically clean up timed-out waiters.

    Avoids memory leak in a common pattern like:

        while True:
            yield condition.wait(short_timeout)
            print('looping....')
    """
    def __init__(self):
        self._waiters = collections.deque()  # Futures.
        self._timeouts = 0

    def _garbage_collect(self):
        # Occasionally clear timed-out waiters.
        self._timeouts += 1
        if self._timeouts > 100:
            self._timeouts = 0
            self._waiters = collections.deque(
                w for w in self._waiters if not w.done())


class Condition(_TimeoutGarbageCollector):
    u"""允许一个或多个协程等待直到被通知的条件.

    就像标准的 `threading.Condition`, 但是不需要一个被获取和释放的底层锁.

    通过 `Condition`, 协程可以等待着被其他协程通知:

    .. testcode::

        from tornado import gen
        from tornado.ioloop import IOLoop
        from tornado.locks import Condition

        condition = Condition()

        @gen.coroutine
        def waiter():
            print("I'll wait right here")
            yield condition.wait()  # Yield a Future.
            print("I'm done waiting")

        @gen.coroutine
        def notifier():
            print("About to notify")
            condition.notify()
            print("Done notifying")

        @gen.coroutine
        def runner():
            # Yield two Futures; wait for waiter() and notifier() to finish.
            yield [waiter(), notifier()]

        IOLoop.current().run_sync(runner)

    .. testoutput::

        I'll wait right here
        About to notify
        Done notifying
        I'm done waiting

    `wait` 有一个可选参数 ``timeout`` , 要不然是一个绝对的时间戳::

        io_loop = IOLoop.current()

        # Wait up to 1 second for a notification.
        yield condition.wait(timeout=io_loop.time() + 1)

    ...或一个 `datetime.timedelta` 相对于当前时间的一个延时::

        # Wait up to 1 second.
        yield condition.wait(timeout=datetime.timedelta(seconds=1))

    这个方法将抛出一个 `tornado.gen.TimeoutError` 如果在最后时间之前都
    没有通知.
    """

    def __init__(self):
        super(Condition, self).__init__()
        self.io_loop = ioloop.IOLoop.current()

    def __repr__(self):
        result = '<%s' % (self.__class__.__name__, )
        if self._waiters:
            result += ' waiters[%s]' % len(self._waiters)
        return result + '>'

    def wait(self, timeout=None):
        """等待 `.notify`.

        返回一个 `.Future` 对象, 如果条件被通知则为 ``True`` ,
        或者在超时之后为 ``False`` .
        """
        waiter = Future()
        self._waiters.append(waiter)
        if timeout:
            def on_timeout():
                waiter.set_result(False)
                self._garbage_collect()
            io_loop = ioloop.IOLoop.current()
            timeout_handle = io_loop.add_timeout(timeout, on_timeout)
            waiter.add_done_callback(
                lambda _: io_loop.remove_timeout(timeout_handle))
        return waiter

    def notify(self, n=1):
        """唤醒 ``n`` 个等待者(waiters) ."""
        waiters = []  # Waiters we plan to run right now.
        while n and self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():  # Might have timed out.
                n -= 1
                waiters.append(waiter)

        for waiter in waiters:
            waiter.set_result(True)

    def notify_all(self):
        """唤醒全部的等待者(waiters) ."""
        self.notify(len(self._waiters))


class Event(object):
    """一个阻塞协程的事件直到它的内部标识设置为True.

    类似于 `threading.Event`.

    协程可以等待一个事件被设置. 一旦它被设置, 调用
    ``yield event.wait()`` 将不会被阻塞除非该事件已经被清除:

    .. testcode::

        from tornado import gen
        from tornado.ioloop import IOLoop
        from tornado.locks import Event

        event = Event()

        @gen.coroutine
        def waiter():
            print("Waiting for event")
            yield event.wait()
            print("Not waiting this time")
            yield event.wait()
            print("Done")

        @gen.coroutine
        def setter():
            print("About to set the event")
            event.set()

        @gen.coroutine
        def runner():
            yield [waiter(), setter()]

        IOLoop.current().run_sync(runner)

    .. testoutput::

        Waiting for event
        About to set the event
        Not waiting this time
        Done
    """
    def __init__(self):
        self._future = Future()

    def __repr__(self):
        return '<%s %s>' % (
            self.__class__.__name__, 'set' if self.is_set() else 'clear')

    def is_set(self):
        """如果内部标识是true将返回 ``True`` ."""
        return self._future.done()

    def set(self):
        """设置内部标识为 ``True``. 所有的等待者(waiters)都被唤醒.

        一旦该标识被设置调用 `.wait` 将不会阻塞.
        """
        if not self._future.done():
            self._future.set_result(None)

    def clear(self):
        """重置内部标识为 ``False``.

        调用 `.wait` 将阻塞直到 `.set` 被调用.
        """
        if self._future.done():
            self._future = Future()

    def wait(self, timeout=None):
        """阻塞直到内部标识为true.

        返回一个Future对象, 在超时之后会抛出一个 `tornado.gen.TimeoutError`
        异常.
        """
        if timeout is None:
            return self._future
        else:
            return gen.with_timeout(timeout, self._future)


class _ReleasingContextManager(object):
    """Releases a Lock or Semaphore at the end of a "with" statement.

        with (yield semaphore.acquire()):
            pass

        # Now semaphore.release() has been called.
    """
    def __init__(self, obj):
        self._obj = obj

    def __enter__(self):
        pass

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._obj.release()


class Semaphore(_TimeoutGarbageCollector):
    """可以在阻塞之前获得固定次数的锁.

    一个信号量管理着代表 `.release` 调用次数减去 `.acquire` 的
    调用次数的计数器, 加一个初始值. 如果必要的话,`.acquire` 方
    法将会阻塞, 直到它可以返回, 而不使该计数器成为负值.

    信号量限制访问共享资源. 为了允许两个worker同时获得权限:

    .. testsetup:: semaphore

       from collections import deque

       from tornado import gen
       from tornado.ioloop import IOLoop
       from tornado.concurrent import Future

       # Ensure reliable doctest output: resolve Futures one at a time.
       futures_q = deque([Future() for _ in range(3)])

       @gen.coroutine
       def simulator(futures):
           for f in futures:
               yield gen.moment
               f.set_result(None)

       IOLoop.current().add_callback(simulator, list(futures_q))

       def use_some_resource():
           return futures_q.popleft()

    .. testcode:: semaphore

        from tornado import gen
        from tornado.ioloop import IOLoop
        from tornado.locks import Semaphore

        sem = Semaphore(2)

        @gen.coroutine
        def worker(worker_id):
            yield sem.acquire()
            try:
                print("Worker %d is working" % worker_id)
                yield use_some_resource()
            finally:
                print("Worker %d is done" % worker_id)
                sem.release()

        @gen.coroutine
        def runner():
            # Join all workers.
            yield [worker(i) for i in range(3)]

        IOLoop.current().run_sync(runner)

    .. testoutput:: semaphore

        Worker 0 is working
        Worker 1 is working
        Worker 0 is done
        Worker 2 is working
        Worker 1 is done
        Worker 2 is done

    Workers 0 和 1 允许并行运行, 但是worker 2将等待直到
    信号量被worker 0释放.

    `.acquire` 是一个上下文管理器, 所以 ``worker`` 可以被写为::

        @gen.coroutine
        def worker(worker_id):
            with (yield sem.acquire()):
                print("Worker %d is working" % worker_id)
                yield use_some_resource()

            # Now the semaphore has been released.
            print("Worker %d is done" % worker_id)

    在 Python 3.5 中, 信号量自身可以作为一个异步上下文管理器::

        async def worker(worker_id):
            async with sem:
                print("Worker %d is working" % worker_id)
                await use_some_resource()

            # Now the semaphore has been released.
            print("Worker %d is done" % worker_id)

    .. versionchanged:: 4.3
       添加对 Python 3.5 ``async with`` 的支持.
    """
    def __init__(self, value=1):
        super(Semaphore, self).__init__()
        if value < 0:
            raise ValueError('semaphore initial value must be >= 0')

        self._value = value

    def __repr__(self):
        res = super(Semaphore, self).__repr__()
        extra = 'locked' if self._value == 0 else 'unlocked,value:{0}'.format(
            self._value)
        if self._waiters:
            extra = '{0},waiters:{1}'.format(extra, len(self._waiters))
        return '<{0} [{1}]>'.format(res[1:-1], extra)

    def release(self):
        """增加counter 并且唤醒一个waiter."""
        self._value += 1
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                self._value -= 1

                # If the waiter is a coroutine paused at
                #
                #     with (yield semaphore.acquire()):
                #
                # then the context manager's __exit__ calls release() at the end
                # of the "with" block.
                waiter.set_result(_ReleasingContextManager(self))
                break

    def acquire(self, timeout=None):
        """递减计数器. 返回一个 Future 对象.

        如果计数器(counter)为0将会阻塞, 等待 `.release`. 在超时之后
        Future 对象将会抛出 `.TimeoutError` .
        """
        waiter = Future()
        if self._value > 0:
            self._value -= 1
            waiter.set_result(_ReleasingContextManager(self))
        else:
            self._waiters.append(waiter)
            if timeout:
                def on_timeout():
                    waiter.set_exception(gen.TimeoutError())
                    self._garbage_collect()
                io_loop = ioloop.IOLoop.current()
                timeout_handle = io_loop.add_timeout(timeout, on_timeout)
                waiter.add_done_callback(
                    lambda _: io_loop.remove_timeout(timeout_handle))
        return waiter

    def __enter__(self):
        raise RuntimeError(
            "Use Semaphore like 'with (yield semaphore.acquire())', not like"
            " 'with semaphore'")

    __exit__ = __enter__

    @gen.coroutine
    def __aenter__(self):
        yield self.acquire()

    @gen.coroutine
    def __aexit__(self, typ, value, tb):
        self.release()


class BoundedSemaphore(Semaphore):
    """一个防止release() 被调用太多次的信号量.

    如果 `.release` 增加信号量的值超过初始值, 它将抛出 `ValueError`.
    信号量通常是通过限制容量来保护资源, 所以一个信号量释放太多次是
    一个错误的标志.
    """
    def __init__(self, value=1):
        super(BoundedSemaphore, self).__init__(value=value)
        self._initial_value = value

    def release(self):
        """增加counter 并且唤醒一个waiter."""
        if self._value >= self._initial_value:
            raise ValueError("Semaphore released too many times")
        super(BoundedSemaphore, self).release()


class Lock(object):
    """协程的锁.

    一个Lock开始解锁, 然后它立即 `acquire` 锁. 虽然它是锁着的,
    一个协程yield `acquire` 并等待, 直到另一个协程调用 `release`.

    释放一个没锁住的锁将抛出 `RuntimeError`.

    在所有Python 版本中 `acquire` 支持上下文管理协议:

    >>> from tornado import gen, locks
    >>> lock = locks.Lock()
    >>>
    >>> @gen.coroutine
    ... def f():
    ...    with (yield lock.acquire()):
    ...        # Do something holding the lock.
    ...        pass
    ...
    ...    # Now the lock is released.

    在Python 3.5, `Lock` 也支持异步上下文管理协议(async context manager
    protocol). 注意在这种情况下没有 `acquire`, 因为
    ``async with`` 同时包含 ``yield`` 和 ``acquire``
    (就像 `threading.Lock`):

    >>> async def f():  # doctest: +SKIP
    ...    async with lock:
    ...        # Do something holding the lock.
    ...        pass
    ...
    ...    # Now the lock is released.

    .. versionchanged:: 3.5
       添加Python 3.5 的 ``async with`` 支持.

    """
    def __init__(self):
        self._block = BoundedSemaphore(value=1)

    def __repr__(self):
        return "<%s _block=%s>" % (
            self.__class__.__name__,
            self._block)

    def acquire(self, timeout=None):
        """尝试锁. 返回一个Future 对象.

        返回一个Future 对象, 在超时之后将抛出
        `tornado.gen.TimeoutError` .
        """
        return self._block.acquire(timeout)

    def release(self):
        """Unlock.

        在队列中等待 `acquire` 的第一个 coroutine 获得锁.

        如果没有锁, 将抛出 `RuntimeError`.
        """
        try:
            self._block.release()
        except ValueError:
            raise RuntimeError('release unlocked lock')

    def __enter__(self):
        raise RuntimeError(
            "Use Lock like 'with (yield lock)', not like 'with lock'")

    __exit__ = __enter__

    @gen.coroutine
    def __aenter__(self):
        yield self.acquire()

    @gen.coroutine
    def __aexit__(self, typ, value, tb):
        self.release()
