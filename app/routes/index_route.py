from flask import Blueprint, render_template, session, redirect, url_for
import uuid
from app.assistant.utils.logging_config import get_logger

index_route_bp = Blueprint('index', __name__)
logger = get_logger(__name__)


@index_route_bp.route('/', methods=['GET', 'POST'])
def index():
    if 'user_id' not in session:
        session['user_id'] = str(uuid.uuid4())
    logger.debug("At index route - redirecting to chatbot")
    return redirect(url_for('chat_bot.chat_bot'))

