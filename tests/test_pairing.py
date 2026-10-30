import pytest

from funpay_manager.security.pairing import PairingChallenge


def test_code_one_use_and_expiry():
    now = [0]
    challenge = PairingChallenge(clock=lambda: now[0])
    assert not challenge.accept("/start", 1, 1, True)
    assert challenge.accept("/start " + challenge.code, 1, 1, True)
    assert not challenge.accept("/start " + challenge.code, 1, 1, True)
    expired = PairingChallenge(ttl=5, clock=lambda: now[0])
    now[0] = 5
    assert not expired.accept("/start " + expired.code, 1, 1, True)


@pytest.mark.parametrize(
    "user,chat,private,bot",
    [
        (1, -100, False, False),
        (1, 2, True, False),
        (1, 1, True, True),
        (0, 0, True, False),
    ],
)
def test_invalid_sender_never_claims(user, chat, private, bot):
    challenge = PairingChallenge()
    assert not challenge.accept("/start " + challenge.code, user, chat, private, bot)
    assert challenge.active


def test_attempt_limit():
    challenge = PairingChallenge(attempts=2)
    assert not challenge.accept("wrong", 1, 1, True)
    assert not challenge.accept("wrong", 1, 1, True)
    assert not challenge.accept("/start " + challenge.code, 1, 1, True)
