"""
feedback_email.py
-----------------
Builds the beta-feedback notification email.

Pure formatting: `format_feedback_email(payload)` turns a validated feedback
dict into a (subject, plain_body, html_body) tuple. It touches no Flask session,
no database, and no network — the SMTP send stays in the app so the app's
rate-limit / session plumbing and the existing tests keep hooking the transport.

Expected `payload` keys (all optional): rating, would_pay, platform, os, mode,
what_worked, what_to_improve, desired_features, other.
"""
from datetime import datetime, timezone
from html import escape as _h


def device_label(platform: str, os_name) -> str:
    """Friendly human-readable device descriptor combining shell + OS."""
    if platform == "android_twa":
        return "Android (installed app)"
    if platform == "ios_pwa":
        return "iPhone/iPad (installed app)"
    if platform == "mobile_browser":
        if os_name == "android":
            return "Android (mobile browser)"
        if os_name == "ios":
            return "iPhone/iPad (mobile browser)"
        return "Mobile browser"
    # platform == "web"
    if os_name == "windows":
        return "Windows laptop / desktop"
    if os_name == "macos":
        return "Mac laptop / desktop"
    if os_name == "linux":
        return "Linux laptop / desktop"
    return "Laptop / desktop"


def mode_label(mode) -> str:
    return {
        "solo": "1:1 Session",
        "couple": "Couple Check-in",
        "group": "Group Circle",
    }.get(mode, "Not in a session")


def pay_label(pay) -> str:
    return {"yes": "Yes", "maybe": "Maybe", "no": "No"}.get(pay, "Not answered")


def stars(rating) -> str:
    if not rating:
        return "Not rated"
    filled = "★" * int(rating)
    empty = "☆" * (5 - int(rating))
    return f"{filled}{empty}"


def format_feedback_email(payload: dict) -> tuple:
    """Build (subject, plain_body, html_body) for the feedback email."""
    rating = payload.get("rating")
    rating_str = f"{rating} / 5" if rating else "N/A"
    pay = payload.get("would_pay")
    platform = payload.get("platform") or ""
    os_name = payload.get("os")
    mode = payload.get("mode")
    device = device_label(platform, os_name)
    _mode_label = mode_label(mode)
    _pay_label = pay_label(pay)
    _stars = stars(rating)
    submitted_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    subject = f"TogetherMindsAI feedback — {_mode_label} on {device} — {rating_str}"

    # ----- Plain-text body (fallback) ---------------------------------------
    def text_section(title: str, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return f"{title}:\n  (none)\n"
        return f"{title}:\n{text}\n"

    plain = (
        f"Rating:           {rating_str}\n"
        f"Would pay:        {_pay_label}\n"
        f"Device:           {device}\n"
        f"Session mode:     {_mode_label}\n"
        f"Submitted at:     {submitted_at}\n"
        f"\n"
        + text_section("What worked well", payload.get("what_worked", ""))
        + "\n"
        + text_section("What could be improved", payload.get("what_to_improve", ""))
        + "\n"
        + text_section("Desired features", payload.get("desired_features", ""))
        + "\n"
        + text_section("Anything else", payload.get("other", ""))
    )

    # ----- HTML body --------------------------------------------------------
    h = _h

    def html_card(title: str, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return (
                f'<h3 style="color:#388E3C;font-size:15px;margin:24px 0 8px;font-weight:600;">{h(title)}</h3>'
                f'<div style="padding:14px 16px;background:#f7faf7;border-left:3px solid #d0d0d0;border-radius:4px;color:#999;font-style:italic;">(none)</div>'
            )
        return (
            f'<h3 style="color:#388E3C;font-size:15px;margin:24px 0 8px;font-weight:600;">{h(title)}</h3>'
            f'<div style="padding:14px 16px;background:#f7faf7;border-left:3px solid #4CAF50;border-radius:4px;white-space:pre-wrap;line-height:1.5;">{h(text)}</div>'
        )

    rating_html = (
        f'<span style="color:#FFC107;font-size:18px;letter-spacing:2px;">{_stars}</span> '
        f'<span style="color:#999;margin-left:6px;">({h(rating_str)})</span>'
    ) if rating else f'<span style="color:#999;font-style:italic;">{h(_stars)}</span>'

    meta_row = (
        '<tr>'
        '<td style="padding:14px 16px;border-bottom:1px solid #e6efe6;width:160px;font-weight:600;color:#555;">{label}</td>'
        '<td style="padding:14px 16px;border-bottom:1px solid #e6efe6;color:#212121;">{value}</td>'
        '</tr>'
    )
    meta_row_last = (
        '<tr>'
        '<td style="padding:14px 16px;width:160px;font-weight:600;color:#555;">{label}</td>'
        '<td style="padding:14px 16px;color:#212121;">{value}</td>'
        '</tr>'
    )

    html_body = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f5f7f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#212121;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7f5;padding:24px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 6px rgba(0,0,0,0.06);">
  <tr><td style="background:#4CAF50;padding:22px 28px;color:#ffffff;">
    <div style="font-size:12px;letter-spacing:0.08em;text-transform:uppercase;opacity:0.9;">TogetherMindsAI</div>
    <div style="font-size:22px;font-weight:600;margin-top:4px;">New feedback received</div>
  </td></tr>

  <tr><td style="padding:24px 28px 8px;">
    <table width="100%" cellpadding="0" cellspacing="0" style="background:#f7faf7;border-radius:8px;border:1px solid #e6efe6;border-collapse:separate;border-spacing:0;">
      {meta_row.format(label='Rating', value=rating_html)}
      {meta_row.format(label='Would pay', value=h(_pay_label))}
      {meta_row.format(label='Device', value=h(device))}
      {meta_row.format(label='Session mode', value=h(_mode_label))}
      {meta_row_last.format(label='Submitted', value=h(submitted_at))}
    </table>
  </td></tr>

  <tr><td style="padding:0 28px 24px;">
    {html_card('What worked well', payload.get('what_worked', ''))}
    {html_card('What could be improved', payload.get('what_to_improve', ''))}
    {html_card('Desired features', payload.get('desired_features', ''))}
    {html_card('Anything else', payload.get('other', ''))}
  </td></tr>

  <tr><td style="padding:14px 24px;background:#fafafa;color:#999;font-size:11px;text-align:center;border-top:1px solid #eee;">
    No IP, no name, no session content captured.
  </td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""

    return subject, plain, html_body
