#!/usr/bin/env python3
import argparse
import collections
import logging
import pathlib
import sys
from typing import Union, Optional, NoReturn
# Local
import accountslib

ACCOUNTS_PATH_DEFAULT = pathlib.Path('~/annex/Info/reference, notes/accounts.txt').expanduser()

DESCRIPTION = """Find how many times each MySudo number has been given out."""
EPILOG = """This counts the number of accounts each phone number has been associated with."""


def make_argparser():
    parser = argparse.ArgumentParser(add_help=False, description=DESCRIPTION, epilog=EPILOG)
    options = parser.add_argument_group('Options')
    options.add_argument('accounts_path', metavar='accounts.txt', type=pathlib.Path, nargs='?',
        default=ACCOUNTS_PATH_DEFAULT,
        help='The accounts file.')
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

    phone_counts = collections.Counter()
    with args.accounts_path.open() as accounts_file:
        for entry in accountslib.parse(accounts_file):
            for account in entry.accounts.values():
                account_phones = set()
                for section in account.values():
                    for key, values in section.items():
                        if key != 'phone':
                            continue
                        for value in values:
                            if value.value.startswith('MySudo '):
                                account_phones.add(value.value)
                            elif value.value.lower().startswith('mysudo'):
                                logging.warning(f'Unusual capitalization/spacing: {value.value}')
                for value in account_phones:
                    phone_counts[value] += 1

    for phone, count in sorted(phone_counts.items()):
        print(f'{phone}:{count:5,d}')

    return None


def fail(error: Union[str,BaseException], code: int = 1) -> NoReturn:
    if __name__ == '__main__':
        logging.critical(f'Error: {error}')
        sys.exit(code)
    elif isinstance(error, BaseException):
        raise error
    else:
        raise RuntimeError(error)


if __name__ == '__main__':
    try:
        sys.exit(main(*sys.argv))
    except BrokenPipeError:
        pass
