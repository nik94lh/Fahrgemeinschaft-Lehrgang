import hashlib
import unittest
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from unittest.mock import patch

from mailimport.parser import MAX_EML_BYTES, MailParseError, parse_eml


COURSE = (
    "Lehrgangsart: Gruppenführer\n"
    "Lehrgangstermin: 10.05.2027 09:45 Uhr – 14.05.2027 15:00 Uhr\n"
    "Schulungsort: SFS Würzburg"
)
APPROVAL = (
    "Sehr geehrter Herr Alex Beispiel,\n\n"
    "Ihre Teilnahme an folgendem Lehrgangstermin wurde genehmigt:\n\n"
    f"{COURSE}\n\n"
    "Die Lehrgangsteilnahme muss spätestens am Sonntag, 18. April 2027 "
    "bestätigt werden. Andernfalls erfolgt am Montag, 19. April 2027 die Stornierung."
)
CONFIRMATION = (
    "Die Lehrgangsanmeldung des Teilnehmers Alex Beispiel für den folgenden "
    f"Lehrgangstermin wurde bestätigt:\n\n{COURSE}"
)


def message(subject, body, subtype="plain"):
    email = EmailMessage()
    email["Subject"] = subject
    email["From"] = "absender@example.invalid"
    email.set_content(body, subtype=subtype)
    return email


class MailParserTests(unittest.TestCase):
    def test_approval_with_conditional_cancellation(self):
        raw = message("Ihre Lehrgangsanmeldung", APPROVAL).as_bytes()
        result = parse_eml(raw)

        self.assertEqual(result.event_type, "approval")
        self.assertEqual(result.participant_name, "Alex Beispiel")
        self.assertEqual(result.course_name, "Gruppenführer")
        self.assertEqual(result.school, "wuerzburg")
        self.assertEqual(result.starts_at.replace(tzinfo=None), datetime(2027, 5, 10, 9, 45))
        self.assertEqual(result.ends_at.replace(tzinfo=None), datetime(2027, 5, 14, 15))
        self.assertEqual(result.starts_at.utcoffset(), timedelta(hours=2))
        self.assertEqual(result.confirmation_deadline, date(2027, 4, 18))
        self.assertEqual(result.cancellation_date, date(2027, 4, 19))
        self.assertEqual(result.fingerprint, hashlib.sha256(raw).hexdigest())
        self.assertEqual(parse_eml(raw), result)
        self.assertEqual(result.warnings, ())

    def test_standalone_past_cancellation_with_generic_subject(self):
        for sentence, event_type in (
            ("Ihre Lehrgangsanmeldung wurde storniert.", "cancellation"),
            ("Ihre Lehrgangsanmeldung wurde automatisch storniert.", "automatic_cancellation"),
        ):
            with self.subTest(sentence=sentence):
                result = parse_eml(message("Ihre Lehrgangsanmeldung", f"{sentence}\n{COURSE}").as_bytes())
                self.assertEqual(result.event_type, event_type)

    def test_negated_cancellation_is_not_a_cancellation(self):
        body = "Ihre Lehrgangsanmeldung wurde nicht storniert."
        for subject in ("Ihre Lehrgangsanmeldung", "Stornierung der Lehrgangsanmeldung"):
            with self.subTest(subject=subject):
                result = parse_eml(message(subject, body).as_bytes())
                self.assertEqual(result.event_type, "unknown")

    def test_conflicting_subject_and_body_leave_event_unknown(self):
        email = message(
            "Teilnahmebestätigung",
            "Ihre Lehrgangsanmeldung wurde automatisch storniert.",
        )
        self.assertEqual(parse_eml(email.as_bytes()).event_type, "unknown")

    def test_generic_cancellation_subject_accepts_automatic_cancellation_body(self):
        email = message("Stornierung", "Ihre Lehrgangsanmeldung wurde automatisch storniert.")
        self.assertEqual(parse_eml(email.as_bytes()).event_type, "automatic_cancellation")

    def test_confirmation_extracts_the_same_party_and_course_details(self):
        result = parse_eml(message("Lehrgangsanmeldung wurde bestätigt", CONFIRMATION).as_bytes())

        self.assertEqual(result.event_type, "confirmation")
        self.assertEqual(result.participant_name, "Alex Beispiel")
        self.assertEqual(result.course_name, "Gruppenführer")
        self.assertEqual(result.school, "wuerzburg")

    def test_html_body_is_parsed_without_accessing_network_or_keeping_links(self):
        html = """
            <html><head><title>Stornierung</title><style>@import url(https://styles.invalid)</style>
            </head><body><script src="https://script.invalid"></script>
            <p>Teilnehmer: Alex Beispiel</p>
            <table><tr><th>Lehrgangsart:</th><td>Gruppenf&uuml;hrer</td></tr>
            <tr><th>Schulungsort:</th><td>SFS Geretsried</td></tr></table>
            <a href="https://link.invalid/token">Bestätigen</a>
            <img src="https://tracking.invalid/pixel">
            <p>Buchungsreferenz: https://secret.invalid/token</p>
            </body></html>
        """
        with (
            patch("socket.socket", side_effect=AssertionError("Netzwerkzugriff")),
            patch("socket.getaddrinfo", side_effect=AssertionError("DNS-Zugriff")),
        ):
            result = parse_eml(message("Teilnahmebestätigung", html, "html").as_bytes())

        self.assertEqual(result.event_type, "confirmation")
        self.assertEqual(result.school, "geretsried")
        self.assertEqual(result.course_name, "Gruppenführer")
        self.assertEqual(result.participant_name, "Alex Beispiel")
        self.assertIsNone(result.booking_reference)
        self.assertNotIn(".invalid", repr(result))

    def test_closing_an_unrelated_html_tag_does_not_expose_hidden_text(self):
        raw = message(
            "Lehrgangsgenehmigung",
            "<head></div><p>Teilnehmer: Falscher Name</p></head>"
            "<p>Teilnehmer: Alex Beispiel</p>",
            "html",
        ).as_bytes()

        self.assertEqual(parse_eml(raw).participant_name, "Alex Beispiel")

    def test_multipart_alternative_prefers_plain_text(self):
        email = message("Lehrgangsgenehmigung", APPROVAL)
        email.add_alternative("<p>Teilnehmer: Anderer Beispielname</p>", subtype="html")

        self.assertEqual(parse_eml(email.as_bytes()).participant_name, "Alex Beispiel")

    def test_plain_text_attachment_is_not_a_message_body(self):
        email = message("Lehrgangsgenehmigung", "Keine Angaben")
        email.add_attachment(APPROVAL, filename="notiz.txt")

        self.assertIsNone(parse_eml(email.as_bytes()).participant_name)

    def test_mime_forward_uses_original_subject_and_body(self):
        forward = message("WG: Lehrgang", "Begleittext: Stornierung")
        forward.add_attachment(message("Teilnahmebestätigung", CONFIRMATION))

        result = parse_eml(forward.as_bytes())

        self.assertEqual(result.event_type, "confirmation")
        self.assertEqual(result.participant_name, "Alex Beispiel")

    def test_multiple_attached_messages_are_ambiguous(self):
        forward = message("WG: Lehrgang", "Zwei verschiedene Nachrichten")
        forward.add_attachment(message("Teilnahmebestätigung", CONFIRMATION))
        forward.add_attachment(message("Lehrgangsgenehmigung", APPROVAL))

        with self.assertRaises(MailParseError):
            parse_eml(forward.as_bytes())

    def test_inline_forward_uses_original_subject(self):
        forwarded = (
            "Alte Notiz\n\n"
            "----- Weitergeleitete Nachricht -----\n"
            "Von: absender@example.invalid\n"
            "Betreff: Teilnahmebestätigung\n\n"
            + "\n".join(f"> {line}" for line in CONFIRMATION.splitlines())
        )
        result = parse_eml(message("WG: Lehrgang", forwarded).as_bytes())

        self.assertEqual(result.event_type, "confirmation")
        self.assertEqual(result.participant_name, "Alex Beispiel")

    def test_inline_html_forward_accepts_envelope_with_layout_spacing(self):
        email = message(
            "WG: Lehrgang",
            "<p>Begleittext</p><p>----- Weitergeleitete Nachricht -----</p>"
            "<p>Von: absender@example.invalid</p><p>Betreff: Teilnahmebestätigung</p>"
            "<p><span>Teilnehmer:</span>\n<span>Alex Beispiel</span></p>",
            "html"
        )

        result = parse_eml(email.as_bytes())

        self.assertEqual(result.event_type, "confirmation")
        self.assertEqual(result.participant_name, "Alex Beispiel")

    def test_reply_does_not_promote_quoted_forward_to_current_event(self):
        body = (
            "Nur zur Kenntnis.\n\n"
            "> ----- Weitergeleitete Nachricht -----\n"
            "> Betreff: Teilnahmebestätigung\n>\n"
            "> Teilnehmer: Alex Beispiel"
        )

        result = parse_eml(message("AW: Lehrgang", body).as_bytes())

        self.assertEqual(result.event_type, "unknown")
        self.assertIsNone(result.participant_name)

    def test_html_reply_does_not_parse_quoted_fields(self):
        email = message(
            "Re: Lehrgang",
            "<p>Vielen Dank.</p><blockquote>"
            "<p>Ihre Teilnahme an folgendem Lehrgangstermin wurde genehmigt:</p>"
            "<p>Teilnehmer: Alex Beispiel</p><p>Schulungsort: SFS Geretsried</p>"
            "</blockquote>",
            "html",
        )
        result = parse_eml(email.as_bytes())

        self.assertEqual(result.event_type, "unknown")
        self.assertIsNone(result.participant_name)
        self.assertIsNone(result.school)

    def test_recognizes_known_subjects_and_leaves_unknown_as_unknown(self):
        examples = {
            "Lehrgangsgenehmigung": "approval",
            "Einladung zum Lehrgang": "invitation",
            "Bitte bestätigen Sie Ihre Lehrgangsteilnahme": "confirmation_request",
            "Teilnahmebestätigung": "confirmation",
            "Ummeldung des Teilnehmers": "reassignment",
            "Stornierung der Lehrgangsanmeldung": "cancellation",
            "Automatische Stornierung": "automatic_cancellation",
            "Terminänderung für den Lehrgang": "schedule_change",
            "Erinnerung an die Bestätigung": "reminder",
            "Rundbrief": "unknown",
        }
        for subject, event_type in examples.items():
            with self.subTest(subject=subject):
                result = parse_eml(message(subject, "Keine Angaben").as_bytes())
                self.assertEqual(result.event_type, event_type)
                self.assertTrue(result.warnings)

    def test_parses_reference_and_previous_participant_from_labeled_fields(self):
        body = "Neuer Teilnehmer: Alex Beispiel\nBisheriger Teilnehmer: Robin Muster\n"
        body += "Buchungsreferenz: LFM-1234/2027\n"
        result = parse_eml(message("Ummeldung", body).as_bytes())

        self.assertEqual(result.participant_name, "Alex Beispiel")
        self.assertEqual(result.previous_participant_name, "Robin Muster")
        self.assertEqual(result.booking_reference, "LFM-1234/2027")

    def test_leaves_conflicting_structured_values_blank(self):
        body = "Teilnehmer: Alex Beispiel\nTeilnehmer: Robin Muster\n"
        body += "Schulungsort: Würzburg und Geretsried\n"
        result = parse_eml(message("Ummeldung", body).as_bytes())

        self.assertIsNone(result.participant_name)
        self.assertIsNone(result.school)
        self.assertTrue(result.warnings)

    def test_invalid_or_ambiguous_berlin_times_remain_unset(self):
        for dates in (
            "32.05.2027 09:45 – 14.05.2027 15:00",
            "10.05.2027 10:00 – 10.05.2027 09:00",
            "28.03.2027 02:30 – 28.03.2027 08:00",
            "31.10.2027 02:30 – 31.10.2027 08:00",
        ):
            with self.subTest(dates=dates):
                raw = message("Einladung", f"Lehrgangstermin: {dates}").as_bytes()
                result = parse_eml(raw)

                self.assertIsNone(result.starts_at)
                self.assertIsNone(result.ends_at)
                self.assertTrue(result.warnings)

    def test_rejects_empty_missing_body_and_oversized_eml(self):
        for raw in (
            b"",
            b"no mail headers here",
            b"Subject: Beispiel\n\n",
            b"x" * (MAX_EML_BYTES + 1),
        ):
            with self.subTest(size=len(raw)), self.assertRaises(MailParseError):
                parse_eml(raw)
