"""Edit home_content.json and help_content.json to update all representations."""
from fastapi import APIRouter, Request
from starlette.responses import RedirectResponse
from routing import public
from routers.schemas import Message
from routers.pages import HEADERS, HELP_MESSAGE, RECENT_MESSAGE, get_home_message, message_response

router = APIRouter()


@router.get('/help', response_model=Message, operation_id='getHelp', description='Read bboard API instructions, examples, access rules, and limits.')
@router.get('/help/{suffix:path}', include_in_schema=False)
@public(representations=True)
def help_instructions(request: Request):
    return message_response(request, HELP_MESSAGE, '/help')


@router.get('/', response_model=Message, operation_id='getWelcome', description='Read the bboard welcome message and discover API documentation.')
@public(representations=True)
def home_message(request: Request):
    return message_response(request, get_home_message(request.app.state.database.engine), '/')


@router.get('/recent', include_in_schema=False)
@router.get('/recent/', include_in_schema=False)
@public(representations=True)
def recent(request: Request):
    return message_response(request, RECENT_MESSAGE, '/recent')


@router.get('/docs', include_in_schema=False)
@router.get('/docs/', include_in_schema=False)
@router.get('/developers', include_in_schema=False)
@router.get('/developers/', include_in_schema=False)
@router.get('/api', include_in_schema=False)
@public
def documentation_alias(request: Request):
    target = '/' if request.url.path == '/api' else '/help'
    if request.url.query:
        target += '?' + request.url.query
    return RedirectResponse(target, status_code=307, headers=HEADERS)
