"""belief_engine.intake.redact — credentials leave, everything around them stays. Invented data only."""
from belief_engine.intake.redact import REDACTED, redact


def test_password_after_is_goes_and_the_account_stays():
    out = redact("Our streaming account is under someone@example.com and password is hunter2.")
    assert out == f"Our streaming account is under someone@example.com and password is {REDACTED}"


def test_password_with_colon_or_equals():
    assert redact("password: s3cret!") == f"password: {REDACTED}"
    assert redact("PIN = 4321 for the garage") == f"PIN = {REDACTED} for the garage"
    assert redact("passcode:abc123") == f"passcode:{REDACTED}"


def test_password_mentioned_without_a_value_is_untouched():
    text = "the password reset email never arrived, so I could not log in"
    assert redact(text) == text


def test_api_key_and_token_values():
    assert redact("api key: abcd1234efgh5678") == f"api key: {REDACTED}"
    assert redact("the token is ZmFrZXRva2VuZm9ydGVzdA") == f"the token is {REDACTED}"


def test_short_token_word_is_untouched():
    text = "a token gesture is enough"
    assert redact(text) == text


def test_keys_by_prefix_anywhere():
    # Suffix kept short and obviously fake: a 24-char one trips GitHub's push protection as a real key.
    assert redact("i pasted sk_live_abcdef123456 into it") == f"i pasted {REDACTED} into it"
    assert redact("key sk-abcdefghijklmnopqrstuvwxyz0123") == f"key {REDACTED}"
    assert redact("AKIAIOSFODNN7EXAMPLE was leaked") == f"{REDACTED} was leaked"
    assert redact("ghp_abcdefghijklmnopqrstuvwxyz123456") == REDACTED
    assert redact("xoxb-123456789012-abcdefghijkl") == REDACTED


def test_ordinary_facts_are_untouched():
    for text in ("the spouse's birthday is August 18.",
                 "Pieter's address is someone@example.com",
                 "i gave it the full stripe api key",
                 "the pin on the map is wrong",
                 "my favorite is poached"):
        assert redact(text) == text


def test_none_and_empty_pass_through():
    assert redact(None) is None
    assert redact("") == ""
