from email import policy
from email.message import EmailMessage
from unittest import TestCase

from mailimport.email_address import normalize_contact_email
from mailimport.parser import parse_eml


APPROVAL = (
    "Sehr geehrte Frau Alex Beispiel,\n\n"
    "Ihre Teilnahme an folgendem Lehrgangstermin wurde genehmigt:\n\n"
    "Lehrgangsart: Gruppenführer\n"
    "Lehrgangstermin: 10.05.2027 09:45 Uhr – 14.05.2027 15:00 Uhr\n"
    "Schulungsort: SFS Würzburg"
)
FUNCTIONAL = ("funktionspostfach@example.invalid",)
BOOKING_SUBJECT = "Ihre Lehrgangsanmeldung"
CONFIRMATION = APPROVAL.replace(
    "Sehr geehrte Frau Alex Beispiel,\n\nIhre Teilnahme an folgendem Lehrgangstermin wurde genehmigt:",
    "Die Lehrgangsanmeldung des Teilnehmers Alex Beispiel für den folgenden Lehrgangstermin wurde bestätigt:",
)


def booking_message(to=None, *, subject=BOOKING_SUBJECT, body=APPROVAL):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "Schulverwaltung <absender@example.invalid>"
    if to is not None:
        message["To"] = to
    message.set_content(body)
    return message


def html_forward(original_headers):
    message = booking_message(subject="WG: Lehrgang")
    original_body = "".join(f"<div>{line}</div>" for line in APPROVAL.splitlines())
    message.set_content(
        "<div>----- Weitergeleitete Nachricht -----</div>" + original_headers + original_body,
        subtype="html",
    )
    return message.as_bytes()


class ParticipantEmailParserTests(TestCase):
    def test_prefers_exact_normalized_display_name_with_multiple_recipients(self):
        message = booking_message(
            "Feuerwehr <stelle@example.invalid>, "
            "ALEX  BEISPIEL <Alex.Beispiel@Example.Invalid>, andere@example.invalid"
        )
        result = parse_eml(message.as_bytes())
        self.assertEqual(result.participant_email, "alex.beispiel@example.invalid")
        self.assertNotIn("alex.beispiel@example.invalid", repr(result))

    def test_unlabeled_recipient_requires_configured_functional_addresses(self):
        message = booking_message("alex@example.invalid")
        self.assertIsNone(parse_eml(message.as_bytes()).participant_email)
        self.assertIsNone(
            parse_eml(message.as_bytes(), functional_addresses=("nur-ein-name",)).participant_email
        )
        self.assertEqual(
            parse_eml(message.as_bytes(), functional_addresses=FUNCTIONAL).participant_email,
            "alex@example.invalid",
        )

    def test_functional_address_is_removed_even_when_display_name_matches(self):
        message = booking_message(
            "Alex Beispiel <FUNKTIONSPOSTFACH@EXAMPLE.INVALID>, Alex@Example.Invalid"
        )
        result = parse_eml(message.as_bytes(), functional_addresses=FUNCTIONAL)
        self.assertEqual(result.participant_email, "alex@example.invalid")

    def test_idn_functional_address_excludes_matching_punycode_recipient(self):
        to = "Alex Beispiel <Buchung@xn--bro-hoa.example>, Alex@Example.Invalid"
        result = parse_eml(
            booking_message(to).as_bytes(), functional_addresses=("BUCHUNG@BÜRO.EXAMPLE",),
        )
        self.assertEqual(result.participant_email, "alex@example.invalid")

    def test_unicode_domain_and_ascii_encoding_share_an_identity(self):
        canonical = "alex@xn--bro-hoa.example"
        self.assertEqual(normalize_contact_email("  ALEX@BÜRO.Example  "), canonical)
        self.assertEqual(normalize_contact_email("Alex@XN--BRO-HOA.Example"), canonical)
        mail = booking_message("Alex Beispiel <ALEX@büro.example>")
        self.assertEqual(parse_eml(mail.as_bytes(policy=policy.SMTPUTF8)).participant_email, canonical)

    def test_named_mismatch_and_multiple_unlabeled_recipients_block_fallback(self):
        for to in (
            "Robin Muster <fremd@example.invalid>",
            "Robin Muster <fremd@example.invalid>, alex@example.invalid",
            "erstes@example.invalid, zweites@example.invalid",
        ):
            with self.subTest(to=to):
                result = parse_eml(booking_message(to).as_bytes(), functional_addresses=FUNCTIONAL)
                self.assertIsNone(result.participant_email)

    def test_multiple_exact_named_matches_are_ambiguous(self):
        message = booking_message(
            "Alex Beispiel <erstes@example.invalid>, Alex Beispiel <zweites@example.invalid>"
        )
        result = parse_eml(message.as_bytes(), functional_addresses=FUNCTIONAL)
        self.assertIsNone(result.participant_email)
        self.assertTrue(result.participant_email_ambiguous)
        warning = "Die E-Mail-Adresse des Teilnehmers ist nicht eindeutig."
        self.assertIn(warning, result.warnings)
        self.assertNotIn("@", warning)

    def test_multiple_unlabeled_candidates_are_ambiguous_but_an_unevidenced_single_one_is_missing(self):
        several = booking_message("erstes@example.invalid, zweites@example.invalid").as_bytes()
        sole = booking_message("erstes@example.invalid").as_bytes()
        excluded = booking_message("funktionspostfach@example.invalid").as_bytes()
        self.assertTrue(parse_eml(several, functional_addresses=FUNCTIONAL).participant_email_ambiguous)
        self.assertFalse(parse_eml(sole).participant_email_ambiguous)
        self.assertFalse(parse_eml(excluded, functional_addresses=FUNCTIONAL).participant_email_ambiguous)

    def test_does_not_infer_address_from_from_cc_or_body(self):
        message = booking_message(body=APPROVAL + "\nE-Mail: alex@example.invalid")
        message.replace_header("From", "Alex Beispiel <absender@example.invalid>")
        message["Cc"] = "Alex Beispiel <kopie@example.invalid>"
        result = parse_eml(message.as_bytes(), functional_addresses=FUNCTIONAL)
        self.assertIsNone(result.participant_email)

    def test_uses_embedded_original_to_over_forwarded_to(self):
        forwarded = booking_message("Alex Beispiel <falsch@example.invalid>", subject="WG: Lehrgang", body="FYI")
        forwarded.add_attachment(booking_message("Alex Beispiel <alex@example.invalid>"))
        result = parse_eml(forwarded.as_bytes(), functional_addresses=FUNCTIONAL)
        self.assertEqual(result.participant_email, "alex@example.invalid")

    def test_uses_original_to_in_explicit_inline_forwarding_envelope(self):
        forwarded = booking_message(
            "Alex Beispiel <falsch@example.invalid>",
            subject="WG: Lehrgang",
            body=(
                "FYI\n\n----- Weitergeleitete Nachricht -----\n"
                "Von: absender@example.invalid\n"
                "An: Alex Beispiel <alex@example.invalid>\n"
                "Betreff: Ihre Lehrgangsanmeldung\n\n"
                + APPROVAL
            ),
        )
        result = parse_eml(forwarded.as_bytes(), functional_addresses=FUNCTIONAL)
        self.assertEqual(result.participant_email, "alex@example.invalid")

    def test_uses_original_to_in_consecutive_html_header_blocks(self):
        raw = html_forward(
            "<div>Von: absender@example.invalid</div>\n"
            "<div>An: Alex Beispiel &lt;alex@example.invalid&gt;</div>\n"
            "<div>Betreff: Ihre Lehrgangsanmeldung</div>\n"
            "<div><br></div>"
        )
        self.assertEqual(parse_eml(raw).participant_email, "alex@example.invalid")

    def test_inline_html_preserves_header_body_gap(self):
        for gap in ("<div></div>", "<br><br>", "<div><br></div>", "<hr>"):
            with self.subTest(gap=gap):
                raw = html_forward(
                    "<div>Von: absender@example.invalid</div>"
                    "<div>Betreff: Ihre Lehrgangsanmeldung</div>"
                    + gap + "<div>An: Alex Beispiel &lt;falsch@example.invalid&gt;</div>"
                )
                self.assertIsNone(parse_eml(raw, functional_addresses=FUNCTIONAL).participant_email)

    def test_envelope_incomplete_or_ambiguous_never_yields_original_to(self):
        for headers in (
            "An: Alex Beispiel <alex@example.invalid>\n",
            (
                "Von: absender@example.invalid\n"
                "An: Alex Beispiel <alex@example.invalid>\n"
                "To: Alex Beispiel <falsch@example.invalid>\n"
            ),
            (
                "Von: absender@example.invalid\n"
                "An:\n"
                "To: Alex Beispiel <falsch@example.invalid>\n"
            ),
        ):
            with self.subTest(headers=headers):
                forwarded = booking_message(
                    "Alex Beispiel <falsch@example.invalid>",
                    subject="WG: Lehrgang",
                    body=(
                        "\n----- Weitergeleitete Nachricht -----\n"
                        + headers + "Betreff: Ihre Lehrgangsanmeldung\n\n" + APPROVAL
                    ),
                )
                result = parse_eml(forwarded.as_bytes(), functional_addresses=FUNCTIONAL)
                self.assertIsNone(result.participant_email)

    def test_inline_to_in_body_after_header_gap_is_not_an_original_header(self):
        forwarded = booking_message(
            subject="WG: Lehrgang",
            body=(
                "----- Weitergeleitete Nachricht -----\n"
                "Von: absender@example.invalid\nBetreff: Ihre Lehrgangsanmeldung\n\n"
                "An: Alex Beispiel <falsch@example.invalid>\n\n"
                + APPROVAL
            ),
        )
        result = parse_eml(forwarded.as_bytes(), functional_addresses=FUNCTIONAL)
        self.assertIsNone(result.participant_email)

    def test_forwarding_without_original_envelope_never_uses_outer_recipient(self):
        message = booking_message("Alex Beispiel <falsch@example.invalid>", subject="WG: Ihre Lehrgangsanmeldung")
        result = parse_eml(message.as_bytes(), functional_addresses=FUNCTIONAL)
        self.assertIsNone(result.participant_email)

    def test_missing_or_ambiguous_participant_and_unknown_status_prevent_capture(self):
        for body in (
            "Ihre Teilnahme an folgendem Lehrgangstermin wurde genehmigt:",
            APPROVAL + "\nTeilnehmer: Alex Beispiel\nTeilnehmer: Robin Muster",
            "Teilnehmer: Alex Beispiel\nSonstiger Inhalt",
        ):
            with self.subTest(body=body):
                message = booking_message("Alex Beispiel <alex@example.invalid>", body=body)
                result = parse_eml(message.as_bytes(), functional_addresses=FUNCTIONAL)
                self.assertIsNone(result.participant_email)

    def test_other_types_do_not_accept_an_unlabeled_recipient(self):
        message = booking_message("alex@example.invalid", subject="Lehrgangsgenehmigung")
        result = parse_eml(message.as_bytes(), functional_addresses=FUNCTIONAL)
        self.assertIsNone(result.participant_email)

    def test_confirmation_subjects_accept_a_named_original_recipient(self):
        for subject in ("Lehrgangsanmeldung wurde bestätigt", "Teilnahmebestätigung"):
            with self.subTest(subject=subject):
                mail = booking_message(
                    "Service <funktionspostfach@example.invalid>, Alex Beispiel <Alex@Example.Invalid>",
                    subject=subject,
                    body=CONFIRMATION,
                )
                result = parse_eml(mail.as_bytes(), functional_addresses=FUNCTIONAL)
                self.assertEqual(result.event_type, "confirmation")
                self.assertEqual(result.participant_email, "alex@example.invalid")

    def test_confirmation_does_not_accept_a_bare_address_or_infer_from_cc(self):
        for recipients in (
            "alex@example.invalid",
            "alex@example.invalid, robin@example.invalid",
            "Alex Beispiel <funktionspostfach@example.invalid>, alex@example.invalid",
        ):
            with self.subTest(recipients=recipients):
                mail = booking_message(recipients, subject="Teilnahmebestätigung", body=CONFIRMATION)
                mail["Cc"] = "Alex Beispiel <cc@example.invalid>"
                result = parse_eml(mail.as_bytes(), functional_addresses=FUNCTIONAL)
                self.assertIsNone(result.participant_email)
                self.assertFalse(result.participant_email_ambiguous)

    def test_confirmation_named_recipients_can_be_ambiguous(self):
        mail = booking_message(
            "Alex Beispiel <a@example.invalid>, Alex Beispiel <b@example.invalid>",
            subject="Teilnahmebestätigung",
            body=CONFIRMATION,
        )
        result = parse_eml(mail.as_bytes())
        self.assertIsNone(result.participant_email)
        self.assertTrue(result.participant_email_ambiguous)

    def test_outlook_inline_forward_supports_separate_valid_semicolon_recipients(self):
        mail = booking_message(
            "Alex Beispiel <outer@example.invalid>",
            subject="WG: Teilnahmebestätigung",
            body=(
                "-----Ursprüngliche Nachricht-----\n"
                "Von: absender@example.invalid\n"
                "Gesendet: Montag, 10. Mai 2027 09:00\n"
                "An: Service <funktionspostfach@example.invalid>; Alex Beispiel <alex@example.invalid>\n"
                "Cc: Alex Beispiel <cc@example.invalid>\n"
                "Betreff: Teilnahmebestätigung\n\n"
                + CONFIRMATION
            ),
        )
        result = parse_eml(mail.as_bytes(), functional_addresses=FUNCTIONAL)
        self.assertEqual(result.participant_email, "alex@example.invalid")

    def test_malformed_outlook_inline_recipient_list_is_ignored(self):
        mail = booking_message(
            "Alex Beispiel <outer@example.invalid>",
            subject="WG: Teilnahmebestätigung",
            body=(
                "-----Ursprüngliche Nachricht-----\n"
                "Von: absender@example.invalid\n"
                "An: Funktion; Alex Beispiel <alex@example.invalid>\n"
                "Betreff: Teilnahmebestätigung\n\n"
                + CONFIRMATION
            ),
        )
        self.assertIsNone(parse_eml(mail.as_bytes(), functional_addresses=FUNCTIONAL).participant_email)

    def test_forwarded_confirmation_never_uses_outer_recipient_as_fallback(self):
        mail = booking_message(
            "Alex Beispiel <outer@example.invalid>",
            subject="WG: Teilnahmebestätigung",
            body=CONFIRMATION,
        )
        self.assertIsNone(parse_eml(mail.as_bytes(), functional_addresses=FUNCTIONAL).participant_email)

    def test_invalid_email_or_duplicate_to_header_never_yields_a_recipient(self):
        invalid_to = booking_message("Alex Beispiel <alex@example>").as_bytes()
        duplicate_to = (
            b"To: Alex Beispiel <wrong@example.invalid>\n"
            + booking_message("Alex Beispiel <alex@example.invalid>").as_bytes()
        )
        for raw in (invalid_to, duplicate_to):
            with self.subTest(raw_prefix=raw[:50]):
                self.assertIsNone(parse_eml(raw, functional_addresses=FUNCTIONAL).participant_email)
