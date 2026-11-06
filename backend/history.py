"""Immutable board revisions. One implementation serves HTTP and MCP readers."""
from datetime import timezone

from fastapi import APIRouter, Path, Query
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text

from realtime import BoardNotFound, DocumentStore
from routers.pages import HEADERS as PAGE_HEADERS, render_html
from routers.schemas import BoardEvent, BoardEvents, Error, ERROR_RESPONSES
from routing import public

router = APIRouter()
HEADERS = {**PAGE_HEADERS, 'X-Robots-Tag': 'noindex, noarchive'}


class RevisionNotFound(LookupError):
    pass


def event_value(row):
    return dict(revision=row['revision'], recorded_at=row['recorded_at'].astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
                kind=row['kind'], content=row['content'], change=row['change'])


class BoardHistory:
    def __init__(self, engine, key):
        self.engine, self.key = engine, key

    def read(self, revision):
        with self.engine.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout = '2s'"))
            row = connection.execute(text('''SELECT e.* FROM app.board_events e
                WHERE board_id=:key AND revision=:revision'''), dict(key=self.key, revision=revision)).mappings().first()
            if row is None:
                if connection.execute(text('SELECT 1 FROM app.boards WHERE id=:key'), dict(key=self.key)).first() is None:
                    raise BoardNotFound()
                raise RevisionNotFound()
        return dict(key=self.key, **event_value(row))

    def list(self, after_revision=-1, through_revision=None, limit=20):
        with self.engine.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout = '2s'"))
            bounds = connection.execute(text('''SELECT min(revision) AS first, max(revision) AS latest
                FROM app.board_events WHERE board_id=:key'''), dict(key=self.key)).mappings().one()
            if bounds['first'] is None:
                raise BoardNotFound()
            upper = bounds['latest'] if through_revision is None else through_revision
            if upper > bounds['latest']:
                raise ValueError('through_revision_not_recorded')
            rows = connection.execute(text('''SELECT * FROM app.board_events WHERE board_id=:key
                AND revision > :after AND revision <= :upper ORDER BY revision LIMIT :count'''),
                dict(key=self.key, after=after_revision, upper=upper, count=limit + 1)).mappings().all()
        more = len(rows) > limit
        events = [event_value(row) for row in rows[:limit]]
        return dict(key=self.key, first_revision=bounds['first'], through_revision=upper, events=events,
                    next_after_revision=events[-1]['revision'] if more else None)


def history_response(request, key, operation, *args):
    try:
        history = BoardHistory(request.app.state.database.engine, key)
        return JSONResponse(getattr(history, operation)(*args), headers=HEADERS)
    except BoardNotFound:
        return JSONResponse({'detail': 'board_not_found'}, status_code=404, headers=HEADERS)
    except RevisionNotFound:
        return JSONResponse({'detail': 'revision_not_recorded'}, status_code=404, headers=HEADERS)
    except ValueError as error:
        return JSONResponse({'detail': str(error)}, status_code=422, headers=HEADERS)
    except Exception:
        return JSONResponse({'detail': 'storage_unavailable'}, status_code=503, headers=HEADERS)


def history_page(request, key):
    """The human history URL is also readable by agents without JavaScript."""
    raw = request.query_params.getlist('revision')
    if raw and (len(raw) != 1 or not raw[0].isascii() or not raw[0].isdigit() or len(raw[0]) > 16 or int(raw[0]) > 9007199254740991):
        return JSONResponse({'detail': 'invalid_revision'}, status_code=422, headers=HEADERS)
    if raw:
        value = history_response(request, key, 'read', int(raw[0]))
    else:
        try:
            current = DocumentStore(request.app.state.database.engine, key).read()['revision']
        except BoardNotFound:
            return JSONResponse({'detail': 'board_not_found'}, status_code=404, headers=HEADERS)
        except Exception:
            return JSONResponse({'detail': 'storage_unavailable'}, status_code=503, headers=HEADERS)
        value = history_response(request, key, 'list', max(-1, current - 20), current, 20)
    if value.status_code != 200:
        return value
    # Reuse the same serialized contract, including explicit UTC timestamps.
    import json
    data = json.loads(value.body)
    events = [data] if raw else list(reversed(data['events']))
    lines = []
    for event in events:
        lines.extend([f"Revision {event['revision']} · {event['kind']} · {event['recorded_at']}",
                      'Saved state:', event['content'] or '(empty board)'])
        change = event['change']
        if change:
            lines.extend([f"Change at character {change['offset']}", 'Removed:', change['removed'] or '(none)',
                          'Inserted:', change['inserted'] or '(none)'])
        else:
            lines.append('History starts here. Earlier versions were not recorded.')
        lines.append('')
    message = dict(title=f"Revision {int(raw[0])}" if raw else 'Board history', content='\n'.join(lines),
                   revision=events[0]['revision'], key=key)
    return HTMLResponse(render_html(message, '/' + key, board=True, history_view=True,
                                   origin=request.app.state.board_site_url), headers=HEADERS)


@router.get('/{board_id}/revisions/{revision}', response_model=BoardEvent, operation_id='readBoardRevision',
    description='Read the complete state and exact change of one immutable revision. recorded_at is server UTC (ISO 8601 with Z). The same board key grants access to all history.',
    responses={**ERROR_RESPONSES, 404: {'description': 'Board or recorded revision not found.', 'model': Error}})
@public
def read_revision(request: Request, board_id: str, revision: int = Path(ge=0, le=9007199254740991)):
    return history_response(request, board_id, 'read', revision)


@router.get('/{board_id}/events', response_model=BoardEvents, operation_id='listBoardEvents',
    description='Read immutable events in revision order, including full resulting states and exact text changes. Start after_revision=-1 for all recorded history. '
        'For subsequent pages preserve through_revision and set after_revision to next_after_revision. A null next_after_revision ends this range. '
        'Later writes cannot change this range. Start another range after through_revision for newer events. Existing boards begin at first_revision (a baseline).',
    responses={**ERROR_RESPONSES, 404: {'description': 'Board not found.', 'model': Error}})
@public
def list_events(request: Request, board_id: str, after_revision: int = Query(-1, ge=-1, le=9007199254740991),
                through_revision: int | None = Query(None, ge=0, le=9007199254740991), limit: int = Query(20, ge=1, le=100)):
    return history_response(request, board_id, 'list', after_revision, through_revision, limit)
