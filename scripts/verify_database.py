"""Verify a runtime role's table permissions, rolling back test data.

Usage: verify_runtime_permissions(pg8000_connection_as_the_runtime_role)
"""
import secrets
from uuid import uuid4

import pg8000.dbapi


def verify_runtime_permissions(connection):
    """Exercise current runtime DML with temporary rows; never commit probe data."""
    board, operation = secrets.token_hex(32), str(uuid4())
    cursor = connection.cursor()
    try:
        cursor.execute("SET LOCAL statement_timeout = '2s'")
        cursor.execute("SET LOCAL lock_timeout = '2s'")
        cursor.execute('INSERT INTO app.boards (id, content) VALUES (%s, %s)', (board, 'permission probe'))
        cursor.execute('UPDATE app.boards SET content=%s, revision=revision+1 WHERE id=%s RETURNING content, revision',
                       ('updated', board))
        if tuple(cursor.fetchone()) != ('updated', 1):
            raise RuntimeError('Board permission probe returned unexpected state')
        cursor.execute('''INSERT INTO app.board_writers (document_id, client_id, sequence, revision)
            VALUES (%s, %s, 1, 1)''', (board, operation))
        cursor.execute('''UPDATE app.board_writers SET sequence=2 WHERE document_id=%s AND client_id=%s
            RETURNING sequence, revision''', (board, operation))
        if tuple(cursor.fetchone()) != (2, 1):
            raise RuntimeError('Writer permission probe returned unexpected state')
        cursor.execute('''INSERT INTO app.board_appends (board_id, operation_id, content_hash, revision)
            VALUES (%s, %s, %s, 1)''', (board, operation, board))
        cursor.execute('SELECT revision FROM app.board_appends WHERE board_id=%s AND operation_id=%s', (board, operation))
        if cursor.fetchone()[0] != 1:
            raise RuntimeError('Append permission probe returned unexpected state')
        cursor.execute('INSERT INTO app.board_creations (operation_id, board_id, content_hash) VALUES (%s, %s, %s)',
                       (operation, board, board))
        cursor.execute('SELECT board_id FROM app.board_creations WHERE operation_id=%s', (operation,))
        if cursor.fetchone()[0] != board:
            raise RuntimeError('Creation permission probe returned unexpected state')
        # 009 must preserve a supplied PATCH span rather than infer another one.
        cursor.execute("SELECT set_config('bboard.change', %s, true)",
                       ('{"offset":0,"removed":"updated","inserted":"updated updated"}',))
        cursor.execute('UPDATE app.boards SET content=%s, revision=revision+1 WHERE id=%s', ('updated updated', board))
        cursor.execute('SELECT revision, content, kind, change FROM app.board_events WHERE board_id=%s ORDER BY revision', (board,))
        events = cursor.fetchall()
        if (len(events) != 3 or tuple(events[0][:3]) != (0, 'permission probe', 'created') or
                tuple(events[1][:3]) != (1, 'updated', 'edit') or events[2][3] !=
                {'offset': 0, 'removed': 'updated', 'inserted': 'updated updated'}):
            raise RuntimeError('Event history permission probe returned unexpected state')
        cursor.execute('''INSERT INTO app.ip_rate_limits
            (ip_hash, second_window, second_count, minute_window, minute_count)
            VALUES (%s, statement_timestamp(), 1, statement_timestamp(), 1)''', (board,))
        cursor.execute('UPDATE app.ip_rate_limits SET second_count=2 WHERE ip_hash=%s RETURNING second_count', (board,))
        if cursor.fetchone()[0] != 2:
            raise RuntimeError('Rate-limit permission probe returned unexpected state')
        cursor.execute('DELETE FROM app.ip_rate_limits WHERE ip_hash=%s RETURNING ip_hash', (board,))
        if cursor.fetchone()[0] != board:
            raise RuntimeError('Rate-limit cleanup permission probe failed')
    finally:
        connection.rollback()
    for operation in ('INSERT INTO app.board_events (board_id, revision, kind, content) VALUES (%s, 0, \'created\', \'forged\')',
                      'UPDATE app.board_events SET content=\'changed\' WHERE board_id=%s',
                      'DELETE FROM app.board_events WHERE board_id=%s'):
        try:
            cursor.execute("SET LOCAL statement_timeout = '2s'")
            cursor.execute(operation, (board,))
        except pg8000.dbapi.DatabaseError as error:
            if error.args[0].get('C') != '42501':
                raise RuntimeError('Runtime must have SELECT-only event permissions') from error
        else:
            raise RuntimeError('Runtime unexpectedly has event mutation privileges')
        finally:
            connection.rollback()
    try:
        cursor.execute("SET LOCAL statement_timeout = '2s'")
        cursor.execute('CREATE TABLE app.must_not_be_allowed (id integer)')
    except pg8000.dbapi.DatabaseError as error:
        if error.args[0].get('C') != '42501':
            raise
    else:
        raise RuntimeError('Runtime unexpectedly has schema creation privileges')
    finally:
        connection.rollback()

