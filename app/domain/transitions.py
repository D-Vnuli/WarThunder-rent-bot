from app.domain.states import AccountStatus, RentalStatus

ACCOUNT_TRANSITIONS: dict[AccountStatus, set[AccountStatus]] = {
    AccountStatus.AVAILABLE: {AccountStatus.RESERVED, AccountStatus.DISABLED},
    AccountStatus.RESERVED: {AccountStatus.ACTIVE, AccountStatus.MANUAL_REVIEW},
    AccountStatus.ACTIVE: {AccountStatus.EXPIRING, AccountStatus.SECURITY_ALERT, AccountStatus.MANUAL_REVIEW},
    AccountStatus.EXPIRING: {AccountStatus.REVOKING, AccountStatus.MANUAL_REVIEW},
    AccountStatus.REVOKING: {AccountStatus.ROTATING_PASSWORD, AccountStatus.MANUAL_REVIEW},
    AccountStatus.ROTATING_PASSWORD: {AccountStatus.AVAILABLE_OFFLINE, AccountStatus.MANUAL_REVIEW},
    AccountStatus.AVAILABLE_OFFLINE: {AccountStatus.AVAILABLE, AccountStatus.MANUAL_REVIEW},
    AccountStatus.SECURITY_ALERT: {AccountStatus.REVOKING, AccountStatus.MANUAL_REVIEW},
    AccountStatus.MANUAL_REVIEW: {AccountStatus.AVAILABLE},
    AccountStatus.DISABLED: {AccountStatus.AVAILABLE},
}


def require_account_transition(current: str, target: AccountStatus) -> None:
    if target not in ACCOUNT_TRANSITIONS.get(AccountStatus(current), set()):
        raise ValueError(f"Illegal account transition: {current} -> {target}")


def require_rental_transition(current: str, target: RentalStatus) -> None:
    allowed = {
        RentalStatus.RESERVED: {RentalStatus.ACTIVE, RentalStatus.MANUAL_REVIEW},
        RentalStatus.ACTIVE: {
            RentalStatus.EXPIRING,
            RentalStatus.REVOKING,
            RentalStatus.SECURITY_TERMINATED,
            RentalStatus.MANUAL_REVIEW,
        },
        RentalStatus.EXPIRING: {RentalStatus.REVOKING, RentalStatus.MANUAL_REVIEW},
        RentalStatus.REVOKING: {RentalStatus.PASSWORD_ROTATION, RentalStatus.MANUAL_REVIEW},
        RentalStatus.PASSWORD_ROTATION: {RentalStatus.FINISHED, RentalStatus.MANUAL_REVIEW},
        RentalStatus.SECURITY_TERMINATED: {RentalStatus.REVOKING, RentalStatus.MANUAL_REVIEW},
    }
    if target not in allowed.get(RentalStatus(current), set()):
        raise ValueError(f"Illegal rental transition: {current} -> {target}")
