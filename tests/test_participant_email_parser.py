from email import policy
from email.message import EmailMessage
from html import escape
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


def forwarded_approval_with_repeated_mailto():
    mail = booking_message(
        "Alex Beispiel <import@example.invalid>",
        subject="Fwd: Ihre Lehrgangsanmeldung",
        body=(
            "Hier die Nachricht\n\n"
            'Von: "schule@example.invalid" <schule@example.invalid<mailto:schule@example.invalid>>\n'
            "Datum: Montag, 10. Mai 2027 um 09:00\n"
            'An: "alex.beispiel@example.invalid" <alex.beispiel@example.invalid<mailto:alex.beispiel@example.invalid>>\n'
            "Cc: Alex Beispiel <cc@example.invalid>\n"
            "Betreff: Ihre Lehrgangsanmeldung\n\n"
            + APPROVAL
        ),
    )
    return mail


class ParticipantEmailParserTests(TestCase):
    def test_prefers_exact_normalized_display_name_with_multiple_recipients(self):
        message = booking_message(
            "Feuerwehr <stelle@example.invalid>, "
            "ALEX  BEISPIEL <Alex.Beispiel@Example.Invalid>, andere@example.invalid"
        )
        result = parse_eml(message.as_bytes())
        self.assertEqual(result.participant_email, "alex.beispiel@example.invalid")
        self.assertNotIn("alex.beispiel@example.invalid", repr(result))

    def test_same_forward_html_can_complete_a_name_only_recipient(self):
        raw = self.multipart_forward("Alex Beispiel", "Alex Beispiel <alex@example.invalid>")
        parsed = parse_eml(raw)
        self.assertEqual(parsed.participant_email, "alex@example.invalid")
        self.assertIn("HTML", parsed.address_note)

    def test_conflicting_alternative_recipients_require_review(self):
        raw = self.multipart_forward(
            "Alex Beispiel <one@example.invalid>", "Alex Beispiel <two@example.invalid>",
        )
        parsed = parse_eml(raw)
        self.assertIsNone(parsed.participant_email)
        self.assertTrue(parsed.participant_email_ambiguous)

    def test_html_must_describe_same_booked_course_and_person(self):
        raw = self.multipart_forward(
            "Alex Beispiel", "Alex Beispiel <alex@example.invalid>",
            html_body=APPROVAL.replace("Gruppenführer", "Zugführer"),
        )
        self.assertIsNone(parse_eml(raw).participant_email)

    def test_reply_with_only_an_attached_booking_does_not_import_it(self):
        mail = EmailMessage()
        mail["Subject"] = "AW: Ihre Lehrgangsanmeldung"
        mail.add_attachment(booking_message("Alex Beispiel <alex@example.invalid>"))
        result = parse_eml(mail.as_bytes())
        self.assertEqual(result.event_type, "unknown")
        self.assertIsNone(result.participant_email)

    def test_named_recipient_can_be_used_for_explicit_approval_subject(self):
        mail = booking_message("Alex Beispiel <alex@example.invalid>", subject="Lehrgangsgenehmigung")
        self.assertEqual(parse_eml(mail.as_bytes()).participant_email, "alex@example.invalid")

    @staticmethod
    def multipart_forward(plain_to, html_to, *, html_body=APPROVAL):
        def forwarded(to, body):
            return (
                "Weiterleitung\n\nVon: Schule <schule@example.invalid>\n"
                "Datum: Montag, 10. Mai 2027 um 09:00\n"
                f"An: {to}\nBetreff: Ihre Lehrgangsanmeldung\n\n{body}"
            )

        mail = booking_message(subject="WG: Ihre Lehrgangsanmeldung", body=forwarded(plain_to, APPROVAL))
        mail.add_alternative(
            "<div>" + escape(forwarded(html_to, html_body)).replace("\n", "<br>") + "</div>",
            subtype="html",
        )
        return mail.as_bytes()

    def test_forwarded_booking_with_date_header_and_repeated_mailto_extracts_personal_address(self):
        mail = forwarded_approval_with_repeated_mailto()
        result = parse_eml(mail.as_bytes())
        self.assertEqual(result.event_type, "approval")
        self.assertEqual(result.participant_email, "alex.beispiel@example.invalid")
        self.assertIsNone(
            parse_eml(mail.as_bytes(), functional_addresses=("alex.beispiel@example.invalid",)).participant_email
        )

    def test_mailbox_display_is_no_evidence_for_another_name_or_confirmation(self):
        for candidate, subject, body in (
            ("andere.person", BOOKING_SUBJECT, APPROVAL),
            ("alex.beispiel", "Teilnahmebestätigung", CONFIRMATION),
        ):
            with self.subTest(subject=subject):
                address = f"{candidate}@example.invalid"
                mail = booking_message(f'"{address}" <{address}>', subject=subject, body=body)
                self.assertIsNone(parse_eml(mail.as_bytes(), functional_addresses=FUNCTIONAL).participant_email)

    def test_mismatching_embedded_mailto_cannot_supply_a_recipient(self):
        mail = booking_message(
            subject="Fwd: Ihre Lehrgangsanmeldung",
            body=(
                "Von: schule@example.invalid\n"
                "Datum: Montag, 10. Mai 2027 um 09:00\n"
                'An: "alex.beispiel@example.invalid" <alex.beispiel@example.invalid<mailto:andere@example.invalid>>\n'
                "Betreff: Ihre Lehrgangsanmeldung\n\n"
                + APPROVAL
            ),
        )
        self.assertIsNone(parse_eml(mail.as_bytes()).participant_email)

    def test_explicit_online_format_preserves_provider_or_uses_online_venue(self):
        for body, school in (
            (APPROVAL + "\nDurchführungsform: Online (Videokonferenz)", "wuerzburg"),
            (APPROVAL.replace("SFS Würzburg", "SFS Würzburg (Online)"), "wuerzburg"),
            (APPROVAL.replace("Gruppenführer", "Gruppenführer (Online)"), "wuerzburg"),
            (APPROVAL.replace("SFS Würzburg", "Online"), "online"),
        ):
            with self.subTest(school=school):
                parsed = parse_eml(booking_message(body=body).as_bytes())
                self.assertTrue(parsed.is_online)
                self.assertEqual(parsed.school, school)

    def test_online_portal_reference_does_not_make_a_class_online(self):
        body = APPROVAL + "\nDie Unterlagen stehen im Online-Portal."
        self.assertFalse(parse_eml(booking_message(body=body).as_bytes()).is_online)

    def test_hybrid_class_is_not_marked_as_online_only(self):
        body = APPROVAL + "\nDurchführungsform: Online und Präsenz"
        self.assertFalse(parse_eml(booking_message(body=body).as_bytes()).is_online)

    def test_unlabeled_recipient_requires_evidence_from_original_to(self):
        message = booking_message("alex@example.invalid")
        self.assertIsNone(parse_eml(message.as_bytes()).participant_email)
        self.assertIsNone(
            parse_eml(message.as_bytes(), functional_addresses=("nur-ein-name",)).participant_email
        )
        self.assertIsNone(parse_eml(message.as_bytes(), functional_addresses=FUNCTIONAL).participant_email)
        exact = booking_message("alex.beispiel@example.invalid").as_bytes()
        self.assertEqual(parse_eml(exact).participant_email, "alex.beispiel@example.invalid")

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

    def test_outlook_html_forward_with_rule_and_no_text_separator(self):
        mail = booking_message("Alex Beispiel <outer@example.invalid>", subject="WG: Lehrgang")
        mail.set_content(
            "<p>Zur Kenntnis</p><hr><div>"
            "<b>Von:</b> Schule &lt;absender@example.invalid&gt;<br>"
            "<b>Gesendet:</b> Montag, 10. Mai 2027 09:00<br>"
            "<b>An:</b> Alex Beispiel &lt;alex@example.invalid&gt;<br>"
            "<b>Betreff:</b> Ihre Lehrgangsanmeldung</div>"
            + "".join(f"<p>{line}</p>" for line in APPROVAL.splitlines() if line),
            subtype="html",
        )
        self.assertEqual(parse_eml(mail.as_bytes()).participant_email, "alex@example.invalid")

    def test_resent_booking_uses_original_to(self):
        mail = booking_message("Alex Beispiel <alex@example.invalid>")
        mail["Resent-Date"] = "Mon, 10 May 2027 09:00:00 +0200"
        mail["Resent-From"] = "forwarder@example.invalid"
        mail["Resent-To"] = "Alex Beispiel <outer@example.invalid>"
        self.assertEqual(parse_eml(mail.as_bytes()).participant_email, "alex@example.invalid")
        del mail["To"]
        self.assertIsNone(parse_eml(mail.as_bytes()).participant_email)

    def test_bare_outlook_header_block_in_reply_does_not_supply_a_contact(self):
        mail = booking_message(
            subject="AW: Ihre Lehrgangsanmeldung",
            body=(
                "Von: absender@example.invalid\n"
                "Gesendet: Montag, 10. Mai 2027 09:00\n"
                "An: Alex Beispiel <alex@example.invalid>\n"
                "Betreff: Ihre Lehrgangsanmeldung\n\n"
                + APPROVAL
            ),
        )
        result = parse_eml(mail.as_bytes())
        self.assertEqual(result.event_type, "unknown")
        self.assertIsNone(result.participant_email)

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
