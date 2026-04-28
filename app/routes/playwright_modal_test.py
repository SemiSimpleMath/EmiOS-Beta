from flask import Blueprint, render_template


playwright_modal_test_bp = Blueprint("playwright_modal_test", __name__)


@playwright_modal_test_bp.route("/playwright_modal_test", methods=["GET"])
def playwright_modal_test():
    return render_template("playwright_modal_test.html")

