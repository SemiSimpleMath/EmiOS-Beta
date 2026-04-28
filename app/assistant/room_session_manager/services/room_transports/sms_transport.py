from __future__ import annotations


class SmsRoomTransport:
    @staticmethod
    def send_reply(*, to_number: str, from_number: str, body: str) -> str:
        from app.services.twilio_sms import TwilioSmsService

        return TwilioSmsService().send_sms(
            to_number=to_number,
            from_number=from_number,
            body=body,
        )
