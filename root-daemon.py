#!/usr/bin/env python3
import argparse
import logging
import os
import pathlib
import pwd
import socket
import subprocess
import sys
from typing import Callable, Optional, NoReturn, TypedDict, Union

DESCRIPTION = """Root daemon providing privileged operations via a Unix domain socket."""

SOCKET_DIR_DEFAULT = pathlib.Path('/run')


def make_argparser():
    parser = argparse.ArgumentParser(add_help=False, description=DESCRIPTION)
    options = parser.add_argument_group('Options')
    options.add_argument('--user',
        help='The user who should be able to write to the socket. Default: the current user.')
    options.add_argument('--socket-path',
        help='Unix domain socket path to listen to. By default, it will be '
            f'{SOCKET_DIR_DEFAULT}/root-daemon.[user].sock, where [user] is the given --user.')
    options.add_argument('-h', '--help', action='help',
        help='Print this argument help text and exit.')
    logs = parser.add_argument_group('Logging')
    logs.add_argument('-l', '--log', type=argparse.FileType('w'), default=sys.stderr,
        help='Print log messages to this file instead of to stderr. Warning: Will overwrite the file.')
    volume = logs.add_mutually_exclusive_group()
    volume.add_argument('-q', '--quiet', dest='volume', action='store_const', const=logging.CRITICAL,
        default=logging.WARNING)
    volume.add_argument('-v', '--verbose', dest='volume', action='store_const', const=logging.INFO)
    volume.add_argument('-D', '--debug', dest='volume', action='store_const', const=logging.DEBUG)
    return parser


def main(*argv: str) -> Optional[int]:

    parser = make_argparser()
    args = parser.parse_args(argv[1:])

    logging.basicConfig(stream=args.log, level=args.volume, format='%(message)s')

    if args.user is None:
        try:
            user = pwd.getpwuid(os.getuid()).pw_name
        except KeyError as error:
            fail(f'Unable to determine current user for uid {os.getuid()}: {error}')
    else:
        user = args.user

    if args.socket_path is None:
        socket_path = SOCKET_DIR_DEFAULT / f'root-daemon.{user}.sock'
    else:
        socket_path = pathlib.Path(args.socket_path)

    run_server(socket_path, user)
    return 0


def run_server(socket_path: pathlib.Path, user: str) -> None:
    """Listen on a Unix domain socket and dispatch privileged operations."""

    try:
        pwent = pwd.getpwnam(user)
    except KeyError as error:
        fail(f"Unknown user {user!r}: {error}")

    uid = pwent.pw_uid
    gid = pwent.pw_gid

    socket_path_str = str(socket_path)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            os.unlink(socket_path_str)
        except FileNotFoundError:
            pass

        server.bind(socket_path_str)
        # Set ownership and restrict access: owner read/write, group read/write.
        try:
            os.chown(socket_path_str, uid, gid)
        except PermissionError as error:
            fail(f'Error: unable to chown socket {socket_path_str!r} to {uid}:{gid}: {error}')

        os.chmod(socket_path_str, 0o660)
        server.listen(5)
        logging.info(f'Listening on socket {socket_path_str!r}.')

        while True:
            conn, _ = server.accept()
            with conn:
                handle_client(conn)
    finally:
        try:
            server.close()
        except Exception:
            pass
        try:
            os.unlink(socket_path_str)
        except FileNotFoundError:
            pass


def handle_client(conn: socket.socket) -> None:
    """Handle a single client connection.

    Protocol: one line per request, UTF-8 text:
      OP_NAME [arg1 arg2 ...]\n
    A single response line is written back.
    """

    reader = conn.makefile(mode='r', encoding='utf-8', errors='replace')
    writer = conn.makefile(mode='w', encoding='utf-8', errors='replace')
    try:
        line = reader.readline()
        if not line:
            return
        text = line.strip()
        if not text:
            logging.error('Received empty request line from client; closing connection.')
            return

        fields = text.split()
        op_name = fields[0].lower()
        op_args = fields[1:]

        op_info = OPERATIONS.get(op_name)
        if op_info is None:
            response = f'ERROR: Unknown operation {op_name!r}'
        else:
            handler = op_info['handler']
            try:
                success, message = handler(op_args)
                if success:
                    status = 'Success'
                else:
                    status = 'ERROR'
                response = f'{status}: {message}'
            except Exception as error:  # noqa: BLE001
                logging.error(f'Error while handling operation {op_name!r}: {error}')
                response = 'ERROR: internal error while handling request'

        if not response.endswith('\n'):
            response += '\n'
        writer.write(response)
        writer.flush()
    finally:
        try:
            reader.close()
        except Exception:
            pass
        try:
            writer.close()
        except Exception:
            pass


def op_shutdown(args: list[str]) -> tuple[bool,str]:
    """Power off the system immediately using systemctl.
    Any arguments are currently ignored.
    """

    logging.info('Shutdown operation requested; invoking systemctl poweroff.')
    try:
        # This assumes a systemd-based system.
        subprocess.run(('systemctl', 'poweroff'), check=True)
    except subprocess.CalledProcessError as error:
        logging.error(f'Error: systemctl poweroff failed: {error}')
        return False, 'shutdown failed'
    except FileNotFoundError as error:
        logging.error(f'Error: systemctl not found: {error}')
        return False, 'systemctl not found'
    return True, 'shutdown initiated'


def fail(error: Union[str,BaseException], code: int = 1) -> NoReturn:
    if __name__ == '__main__':
        logging.critical(f'Error: {error}')
        sys.exit(code)
    elif isinstance(error, BaseException):
        raise error
    else:
        raise RuntimeError(error)

Handler = Callable[[list[str]], tuple[bool, str]]


class Operation(TypedDict):
    handler: Handler
    help: str


OPERATIONS: dict[str, Operation] = {
    'SHUTDOWN': {
        'handler': op_shutdown,
        'help': 'Power off the system immediately.',
    },
}


if __name__ == '__main__':
    try:
        sys.exit(main(*sys.argv))
    except BrokenPipeError:
        pass
