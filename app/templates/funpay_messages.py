from datetime import datetime


def credential_message(login: str, password: str, expires_at: datetime) -> str:
    return (
        f"Доступ активирован до {expires_at.isoformat()}.\n"
        f"Логин: {login}\nПароль: {password}\n"
        "Если игра запросит код подтверждения, напишите в этот чат: код"
    )
