def normalize_phone_number(phone_number: str) -> str:
    """Convert phone number to E.164 format for Uganda (+256)."""
    if not phone_number:
        return ""
    phone_number = phone_number.strip().replace(" ", "")
    if phone_number.startswith("0"):
        return "+256" + phone_number[1:]
    elif phone_number.startswith("256"):
        return "+" + phone_number
    elif not phone_number.startswith("+256"):
        return "+256" + phone_number
    return phone_number
