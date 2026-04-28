from flask import Flask, render_template
from flask import Blueprint, redirect, url_for
main_bp = Blueprint('main', __name__)

@main_bp.route('/main', methods=['GET', 'POST'])
def main():
    # Redirect to chatbot
    return redirect(url_for('chat_bot.chat_bot'))



