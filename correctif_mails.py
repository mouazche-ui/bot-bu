"""Lecture complète des mails Affluences du bot BU."""

import re
import sys

import reserver as bot

ORIGINAL_PARSE_SLOT = bot.parse_slot
ORIGINAL_PARSE_MESSAGE = bot.parse_message


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def clean_mime(node):
    if not isinstance(node, dict):
        return {}

    result = dict(node)

    if result.get("ct"):
        result["ct"] = result["ct"].split(";", 1)[0].lower().strip()

    result["mp"] = [
        clean_mime(part)
        for part in as_list(result.get("mp"))
    ]

    return result


def expanded_slot(text):
    text = str(text)
    text = text.replace("\u202f", " ").replace("\xa0", " ")
    text = text.replace("—", "-").replace("−", "-")

    text = re.sub(
        r"\b(\d{1,2})(st|nd|rd|th)\b",
        r"\1",
        text,
        flags=re.I,
    )

    text = re.sub(
        r"\b([ap])\.?\s*m\.?\b",
        r"\1m",
        text,
        flags=re.I,
    )

    text = re.sub(
        r"\s+(?:until|till)\s+",
        " to ",
        text,
        flags=re.I,
    )

    text = re.sub(
        r"(\d)\s*([:h])\s*(\d{2})",
        r"\1\2\3",
        text,
    )

    text = re.sub(
        r"\b(\d{1,2}),\s*(20\d{2})\b",
        r"\1 \2",
        text,
    )

    try:
        return ORIGINAL_PARSE_SLOT(text)
    except (ValueError, TypeError):
        return None


def normalized_message(message):
    message = clean_mime(message)

    message["e"] = [
        dict(address)
        for address in as_list(message.get("e"))
        if isinstance(address, dict)
    ]

    for address in message["e"]:
        if isinstance(address.get("a"), str):
            address["a"] = address["a"].strip().lower()

    subject = bot.string_value(message.get("su"))
    subject = subject.replace("\u202f", " ")
    subject = subject.replace("\xa0", " ").replace("—", "-")
    message["su"] = subject

    return message


def parse_message(message, account):
    return ORIGINAL_PARSE_MESSAGE(
        normalized_message(message),
        account,
    )


def safe_text(text):
    text = re.sub(
        r"https?://\S+",
        "[lien masqué]",
        str(text),
    )

    text = re.sub(
        r"[\w.+-]+@[\w.-]+",
        "[adresse masquée]",
        text,
    )

    text = re.sub(
        r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b",
        "[jeton masqué]",
        text,
        flags=re.I,
    )

    text = re.sub(
        r"\b[A-Za-z0-9_=-]{32,}\b",
        "[valeur masquée]",
        text,
    )

    return " ".join(text.split())


def explain(message, account, number):
    message = normalized_message(message)
    subject = bot.string_value(message.get("su"))

    reader = bot.MailHTML()
    reader.feed(" ".join(bot.mime_contents(message)))
    text = " ".join(reader.parts)

    senders = [
        address.get("a", "")
        for address in message["e"]
        if address.get("t") == "f"
    ]

    recipients = [
        address.get("a", "")
        for address in message["e"]
        if address.get("t") in {"t", "c"}
    ]

    reasons = []

    if "no-reply@affluences.com" not in senders:
        reasons.append("expéditeur non reconnu")

    recipient_matches = any(
        bot.base_email(address) == bot.base_email(account.email)
        for address in recipients
        if "@" in address
    )

    if not recipient_matches:
        reasons.append(
            "destinataire différent du compte configuré ou absent"
        )

    if not text.strip():
        reasons.append("corps du mail absent")
    elif not expanded_slot(text):
        reasons.append("date ou horaires non reconnus")

    if not re.search(
        r"BU Joseph-Fourier\s*[-–:]\s*(.+)$",
        subject,
        re.I,
    ):
        reasons.append("nom de salle non reconnu dans l'objet")

    if not reasons:
        reasons.append("statut ou modèle de message non reconnu")

    bot.log(f"Compte {number}: détail du mail bloquant")
    bot.log("Objet: " + safe_text(subject)[:180])
    bot.log("Motif: " + "; ".join(reasons))

    if text.strip() and not expanded_slot(text):
        months = "|".join(bot.MONTHS)

        match = re.search(
            r"\b(?:" + months + r")\b",
            bot.norm(text),
        )

        if match:
            begin = max(0, match.start() - 55)
            excerpt = text[begin:begin + 220]
            bot.log("Extrait date: " + safe_text(excerpt))


async def full_messages(self):
    parsed = []
    offset = 0

    for _ in range(30):
        data = await self.search(offset)
        messages = as_list(data.get("m"))

        for item in messages:
            identifier = str(item["id"])

            if identifier not in self.read_cache:
                detail = await self.soap(
                    "GetMsgRequest",
                    {
                        "m": {
                            "id": identifier,
                            "html": 1,
                            "read": 0,
                            "max": 200000,
                        }
                    },
                )

                full = as_list(detail.get("m"))

                if len(full) != 1 or not isinstance(full[0], dict):
                    raise RuntimeError(
                        f"Compte {self.number}: "
                        "réponse GetMsg incomplète"
                    )

                message = normalized_message(full[0])
                value = parse_message(message, self.account)
                subject = bot.string_value(message.get("su"))

                if (
                    value is None
                    and "bu joseph-fourier" in bot.norm(subject)
                ):
                    reader = bot.MailHTML()
                    reader.feed(
                        " ".join(bot.mime_contents(message))
                    )

                    slot = expanded_slot(
                        " ".join(reader.parts)
                    )

                    if (
                        slot is None
                        or slot[0] >= bot.now().date().isoformat()
                    ):
                        explain(
                            message,
                            self.account,
                            self.number,
                        )

                        raise RuntimeError(
                            f"Compte {self.number}: "
                            "lecture du mail à corriger; "
                            "voir Objet et Motif ci-dessus"
                        )

                self.read_cache[identifier] = value

            if self.read_cache[identifier]:
                parsed.append(self.read_cache[identifier])

        more = str(data.get("more", "0")).lower()

        if more not in {"1", "true"}:
            return sorted(
                parsed,
                key=lambda value: value[0].updated,
                reverse=True,
            )

        if not messages:
            raise RuntimeError(
                "Pagination du webmail incohérente"
            )

        offset += len(messages)

    raise RuntimeError(
        "Analyse des mails incomplète: plus de 3000 résultats"
    )


def install():
    bot.parse_slot = expanded_slot
    bot.parse_message = parse_message
    bot.Mailbox.messages = full_messages


if __name__ == "__main__":
    install()
    sys.exit(bot.main())
