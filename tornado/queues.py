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

__all__ = ['Queue', 'PriorityQueue', 'LifoQueue', 'QueueFull', 'QueueEmpty']

import collections
import heapq

from tornado import gen, ioloop
from tornado.concurrent import Future
from tornado.locks import Event


class QueueEmpty(Exception):
    """当队列中没有项目时, 由 `.Queue.get_nowait` 抛出."""
    pass


class QueueFull(Exception):
    """当队列为最大size时, 由 `.Queue.put_nowait` 抛出."""
    pass


def _set_timeout(future, timeout):
    if timeout:
        def on_timeout():
            future.set_exception(gen.TimeoutError())
        io_loop = ioloop.IOLoop.current()
        timeout_handle = io_loop.add_timeout(timeout, on_timeout)
        future.add_done_callback(
            lambda _: io_loop.remove_timeout(timeout_handle))


class _QueueIterator(object):
    def __init__(self, q):
        self.q = q

    def __anext__(self):
        return self.q.get()


class Queue(object):
    """协调生产者消费者协程.

    如果maxsize 是0(默认配置)意味着队列的大小是无限的.

    .. testcode::

        from tornado import gen
        from tornado.ioloop import IOLoop
        from tornado.queues import Queue

        q = Queue(maxsize=2)

        @gen.coroutine
        def consumer():
            while True:
                item = yield q.get()
                try:
                    print('Doing work on %s' % item)
                    yield gen.sleep(0.01)
                finally:
                    q.task_done()

        @gen.coroutine
        def producer():
            for item in range(5):
                yield q.put(item)
                print('Put %s' % item)

        @gen.coroutine
        def main():
            # Start consumer without waiting (since it never finishes).
            IOLoop.current().spawn_callback(consumer)
            yield producer()     # Wait for producer to put all tasks.
            yield q.join()       # Wait for consumer to finish all tasks.
            print('Done')

        IOLoop.current().run_sync(main)

    .. testoutput::

        Put 0
        Put 1
        Doing work on 0
        Put 2
        Doing work on 1
        Put 3
        Doing work on 2
        Put 4
        Doing work on 3
        Doing work on 4
        Done

    在Python 3.5, `Queue` 实现了异步迭代器协议, 所以
    ``consumer()`` 可以被重写为::

        async def consumer():
            async for item in q:
                try:
                    print('Doing work on %s' % item)
                    yield gen.sleep(0.01)
                finally:
                    q.task_done()

    .. versionchanged:: 4.3
       为Python 3.5添加 ``async for`` 支持 in Python 3.5.

    """
    def __init__(self, maxsize=0):
        if maxsize is None:
            raise TypeError("maxsize can't be None")

        if maxsize < 0:
            raise ValueError("maxsize can't be negative")

        self._maxsize = maxsize
        self._init()
        self._getters = collections.deque([])  # Futures.
        self._putters = collections.deque([])  # Pairs of (item, Future).
        self._unfinished_tasks = 0
        self._finished = Event()
        self._finished.set()

    @property
    def maxsize(self):
        """队列中允许的最大项目数."""
        return self._maxsize

    def qsize(self):
        """当前队列中的项目数."""
        return len(self._queue)

    def empty(self):
        return not self._queue

    def full(self):
        if self.maxsize == 0:
            return False
        else:
            return self.qsize() >= self.maxsize

    def put(self, item, timeout=None):
        """将一个项目放入队列中, 可能需要等待直到队列中有空间.

        返回一个Future对象, 如果超时会抛出 `tornado.gen.TimeoutError` .
        """
        try:
            self.put_nowait(item)
        except QueueFull:
            future = Future()
            self._putters.append((item, future))
            _set_timeout(future, timeout)
            return future
        else:
            return gen._null_future

    def put_nowait(self, item):
        """非阻塞的将一个项目放入队列中.

        如果没有立即可用的空闲插槽, 则抛出 `QueueFull`.
        """
        self._consume_expired()
        if self._getters:
            assert self.empty(), "queue non-empty, why are getters waiting?"
            getter = self._getters.popleft()
            self.__put_internal(item)
            getter.set_result(self._get())
        elif self.full():
            raise QueueFull
        else:
            self.__put_internal(item)

    def get(self, timeout=None):
        """从队列中删除并返回一个项目.

        返回一个Future对象, 当项目可用时resolve, 或者在超时后抛出
        `tornado.gen.TimeoutError` .
        """
        future = Future()
        try:
            future.set_result(self.get_nowait())
        except QueueEmpty:
            self._getters.append(future)
            _set_timeout(future, timeout)
        return future

    def get_nowait(self):
        """非阻塞的从队列中删除并返回一个项目.

        如果有项目是立即可用的则返回该项目, 否则抛出 `QueueEmpty`.
        """
        self._consume_expired()
        if self._putters:
            assert self.full(), "queue not full, why are putters waiting?"
            item, putter = self._putters.popleft()
            self.__put_internal(item)
            putter.set_result(None)
            return self._get()
        elif self.qsize():
            return self._get()
        else:
            raise QueueEmpty

    def task_done(self):
        """表明前面排队的任务已经完成.

        被消费者队列使用. 每个 `.get` 用来获取一个任务, 随后(subsequent)
        调用 `.task_done` 告诉队列正在处理的任务已经完成.

        如果 `.join` 正在阻塞, 它会在所有项目都被处理完后调起;
        即当每个 `.put` 都被一个 `.task_done` 匹配.

        如果调用次数超过 `.put` 将会抛出 `ValueError` .
        """
        if self._unfinished_tasks <= 0:
            raise ValueError('task_done() called too many times')
        self._unfinished_tasks -= 1
        if self._unfinished_tasks == 0:
            self._finished.set()

    def join(self, timeout=None):
        """阻塞(block)直到队列中的所有项目都处理完.

        返回一个Future对象, 超时后会抛出 `tornado.gen.TimeoutError` 异常.
        """
        return self._finished.wait(timeout)

    @gen.coroutine
    def __aiter__(self):
        return _QueueIterator(self)

    # These three are overridable in subclasses.
    def _init(self):
        self._queue = collections.deque()

    def _get(self):
        return self._queue.popleft()

    def _put(self, item):
        self._queue.append(item)
    # End of the overridable methods.

    def __put_internal(self, item):
        self._unfinished_tasks += 1
        self._finished.clear()
        self._put(item)

    def _consume_expired(self):
        # Remove timed-out waiters.
        while self._putters and self._putters[0][1].done():
            self._putters.popleft()

        while self._getters and self._getters[0].done():
            self._getters.popleft()

    def __repr__(self):
        return '<%s at %s %s>' % (
            type(self).__name__, hex(id(self)), self._format())

    def __str__(self):
        return '<%s %s>' % (type(self).__name__, self._format())

    def _format(self):
        result = 'maxsize=%r' % (self.maxsize, )
        if getattr(self, '_queue', None):
            result += ' queue=%r' % self._queue
        if self._getters:
            result += ' getters[%s]' % len(self._getters)
        if self._putters:
            result += ' putters[%s]' % len(self._putters)
        if self._unfinished_tasks:
            result += ' tasks=%s' % self._unfinished_tasks
        return result


class PriorityQueue(Queue):
    """一个有优先级的 `.Queue` 最小的最优先.

    写入的条目通常是元组, 类似 ``(priority number, data)``.

    .. testcode::

        from tornado.queues import PriorityQueue

        q = PriorityQueue()
        q.put((1, 'medium-priority item'))
        q.put((0, 'high-priority item'))
        q.put((10, 'low-priority item'))

        print(q.get_nowait())
        print(q.get_nowait())
        print(q.get_nowait())

    .. testoutput::

        (0, 'high-priority item')
        (1, 'medium-priority item')
        (10, 'low-priority item')
    """
    def _init(self):
        self._queue = []

    def _put(self, item):
        heapq.heappush(self._queue, item)

    def _get(self):
        return heapq.heappop(self._queue)


class LifoQueue(Queue):
    """一个后进先出(Lifo)的 `.Queue`.

    .. testcode::

        from tornado.queues import LifoQueue

        q = LifoQueue()
        q.put(3)
        q.put(2)
        q.put(1)

        print(q.get_nowait())
        print(q.get_nowait())
        print(q.get_nowait())

    .. testoutput::

        1
        2
        3
    """
    def _init(self):
        self._queue = []

    def _put(self, item):
        self._queue.append(item)

    def _get(self):
        return self._queue.pop()
