"""The futures deployment's webhook sender: step 4 of the futures plan fills this in (the futures token, the kill
switch, the order log, the send window, the timeout handling). Here it is a stub: `send` refuses every order
(NotImplementedError, which the book logs as a failed order and never retries), and `kill_switch` only records that
the book asked for one. The book calls either only when a side's Real switch is on."""

sent_kills: list[dict] = []                 # every kill_switch=1 the book asked for (the stub's record)


def send(symbol: str, action: str, lots=None, gap: float = 10.0) -> str:
    """One signal to the futures deployment: sig_symbol (the NSE symbol), sig_qty in lots (not for EXIT),
    sig_action BUY | SELL | EXIT. Not implemented yet: the real sender comes in step 4."""
    raise NotImplementedError("the futures sender is not wired yet (step 4): nothing was sent")


def kill_switch(note: str = "") -> str:
    """kill_switch=1: closes everything at the deployment and stops its entries. The stub records the request."""
    from backend.database.models import now_ist
    sent_kills.append({"at": now_ist(), "note": note})
    return "recorded (stub): kill_switch=1 not sent"
