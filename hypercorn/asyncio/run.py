import asyncio
import os
import platform
import signal
import sys
import warnings
from multiprocessing import Event, Process
from multiprocessing.synchronize import Event as EventType
from socket import (
    AF_INET,
    AF_INET6,
    fromfd as socket_fromfd,
    SO_REUSEADDR,
    SOCK_STREAM,
    socket,
    SOL_SOCKET,
)
from typing import Any, Optional, Type

from ..asgi.run import H2CProtocolRequired, WebsocketProtocolRequired
from ..config import Config
from ..typing import ASGIFramework
from ..utils import load_application, MustReloadException, observe_changes, write_pid_file
from .base import HTTPServer
from .h2 import H2Server
from .h11 import H11Server
from .lifespan import Lifespan
from .wsproto import WebsocketServer

try:
    from socket import AF_UNIX
except ImportError:
    AF_UNIX = None


class Shutdown(SystemExit):
    code = 1


def _raise_shutdown(*args: Any) -> None:
    raise Shutdown()


async def _check_shutdown(shutdown_event: EventType) -> None:
    while True:
        if shutdown_event.is_set():
            raise Shutdown()
        await asyncio.sleep(0.1)


class Server(asyncio.Protocol):
    def __init__(
        self, app: Type[ASGIFramework], loop: asyncio.AbstractEventLoop, config: Config
    ) -> None:
        self.app = app
        self.loop = loop
        self.config = config
        self._server: Optional[HTTPServer] = None
        self._ssl_enabled = False

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        ssl_object = transport.get_extra_info("ssl_object")
        if ssl_object is not None:
            self._ssl_enabled = True
            protocol = ssl_object.selected_alpn_protocol()
        else:
            protocol = "http/1.1"

        if protocol == "h2":
            self._server = H2Server(self.app, self.loop, self.config, transport)
        else:
            self._server = H11Server(self.app, self.loop, self.config, transport)

    def connection_lost(self, exception: Exception) -> None:
        self._server.connection_lost(exception)

    def data_received(self, data: bytes) -> None:
        try:
            self._server.data_received(data)
        except WebsocketProtocolRequired as error:
            self._server = WebsocketServer(
                self.app,
                self.loop,
                self.config,
                self._server.transport,
                upgrade_request=error.request,
            )
        except H2CProtocolRequired as error:
            self._server = H2Server(
                self.app,
                self.loop,
                self.config,
                self._server.transport,
                upgrade_request=error.request,
            )

    def eof_received(self) -> bool:
        if self._ssl_enabled:
            # Returning anything other than False has no affect under
            # SSL, and just raises an annoying warning.
            return False
        return self._server.eof_received()

    def pause_writing(self) -> None:
        self._server.pause_writing()

    def resume_writing(self) -> None:
        self._server.resume_writing()


async def _windows_signal_support() -> None:
    # See https://bugs.python.org/issue23057, to catch signals on
    # Windows it is necessary for an IO event to happen periodically.
    while True:
        await asyncio.sleep(1)


def run_single(
    app: Type[ASGIFramework],
    config: Config,
    *,
    loop: asyncio.AbstractEventLoop,
    sock: Optional[socket] = None,
    is_child: bool = False,
    shutdown_event: Optional[EventType] = None,
) -> None:
    """Create a server to run the app on given the options.

    Arguments:
        app: The ASGI Framework to run.
        config: The configuration that defines the server.
        loop: Asyncio loop to create the server in, if None, take default one.
    """
    if loop is None:
        warnings.warn("Event loop is not specified, this can cause unexpected errors")
        loop = asyncio.get_event_loop()

    loop.set_debug(config.debug)

    lifespan = Lifespan(app, config)
    lifespan_task = asyncio.ensure_future(lifespan.handle_lifespan())

    loop.run_until_complete(lifespan.wait_for_startup())

    ssl_context = config.create_ssl_context()

    if sock is not None:
        create_server = loop.create_server(
            lambda: Server(app, loop, config), ssl=ssl_context, sock=sock, reuse_port=is_child
        )
    elif config.file_descriptor is not None:
        sock = socket_fromfd(config.file_descriptor, AF_UNIX, SOCK_STREAM)
        create_server = loop.create_server(
            lambda: Server(app, loop, config), ssl=ssl_context, sock=sock
        )
    elif config.unix_domain is not None:
        create_server = loop.create_unix_server(
            lambda: Server(app, loop, config), config.unix_domain, ssl=ssl_context
        )
    else:
        create_server = loop.create_server(
            lambda: Server(app, loop, config),
            host=config.host,
            port=config.port,
            ssl=ssl_context,
            reuse_port=is_child,
        )
    server = loop.run_until_complete(create_server)

    if platform.system() == "Windows":
        loop.create_task(_windows_signal_support())

    if shutdown_event is not None:
        loop.create_task(_check_shutdown(shutdown_event))
    else:
        for signal_name in {"SIGINT", "SIGTERM", "SIGBREAK"}:
            if hasattr(signal, signal_name):
                signal.signal(getattr(signal, signal_name), _raise_shutdown)

    reload_ = False
    try:
        if config.use_reloader:
            loop.run_until_complete(observe_changes(asyncio.sleep))
        else:
            loop.run_forever()
    except MustReloadException:
        reload_ = True
    except (SystemExit, KeyboardInterrupt):
        pass
    finally:
        server.close()
        loop.run_until_complete(server.wait_closed())
        _cancel_all_other_tasks(loop, lifespan_task)
        loop.run_until_complete(loop.shutdown_asyncgens())

        try:
            loop.remove_signal_handler(signal.SIGINT)
            loop.remove_signal_handler(signal.SIGTERM)
        except NotImplementedError:
            pass  # Unix only

        loop.run_until_complete(lifespan.wait_for_shutdown())
        lifespan_task.cancel()
        loop.run_until_complete(lifespan_task)
        loop.close()

    if reload_:
        # Restart this process (only safe for dev/debug)
        os.execv(sys.executable, [sys.executable] + sys.argv)


def run_multiple(config: Config) -> None:
    """Create a server to run as specified in teh config.

    Arguments:
        config: The configuration that defines the server.
    """
    if config.use_reloader:
        raise RuntimeError("Reloader can only be used with a single worker")

    if config.unix_domain is not None:
        sock = socket(AF_UNIX)
        sock.bind(config.unix_domain)
    elif config.file_descriptor is not None:
        sock = socket_fromfd(config.file_descriptor, AF_UNIX, SOCK_STREAM)
    else:
        sock = socket(AF_INET6 if ":" in config.host else AF_INET)
        sock.bind((config.host, config.port))
    sock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
    sock.set_inheritable(True)  # type: ignore

    processes = []

    # Ignore SIGINT before creating the processes, so that they
    # inherit the signal handling. This means that the shutdown
    # function controls the shutdown.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    shutdown_event = Event()

    for _ in range(config.workers):
        process = Process(
            target=_run_worker,
            kwargs={"config": config, "shutdown_event": shutdown_event, "sock": sock},
        )
        process.daemon = True
        process.start()
        processes.append(process)

    def shutdown(*args: Any) -> None:
        shutdown_event.set()

    for signal_name in {"SIGINT", "SIGTERM", "SIGBREAK"}:
        if hasattr(signal, signal_name):
            signal.signal(getattr(signal, signal_name), shutdown)

    for process in processes:
        process.join()
    for process in processes:
        process.terminate()

    sock.close()


def _run_worker(config: Config, shutdown_event: EventType, sock: Optional[socket] = None) -> None:
    if config.worker_class == "uvloop":
        try:
            import uvloop
        except ImportError as error:
            raise Exception("uvloop is not installed") from error
        else:
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = load_application(config.application_path)
    run_single(app, config, loop=loop, sock=sock, is_child=True, shutdown_event=shutdown_event)


def run(config: Config) -> None:
    if config.pid_path is not None:
        write_pid_file(config.pid_path)

    if config.workers == 1:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app = load_application(config.application_path)
        run_single(app, config, loop=loop)
    else:
        run_multiple(config)


def _cancel_all_other_tasks(
    loop: asyncio.AbstractEventLoop, protected_task: asyncio.Future
) -> None:
    tasks = [task for task in asyncio.tasks.all_tasks(loop) if task != protected_task]
    for task in tasks:
        task.cancel()
    loop.run_until_complete(asyncio.gather(*tasks, loop=loop, return_exceptions=True))

    for task in tasks:
        if task.cancelled():
            continue
        if task.exception() is not None:
            loop.call_exception_handler(
                {
                    "message": "unhandled exception during asyncio.run() shutdown",
                    "exception": task.exception(),
                    "task": task,
                }
            )
