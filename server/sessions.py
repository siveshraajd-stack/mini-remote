class Session:
    def __init__(self, sid, pc_a, pc_b, sender):
        self.sid, self.pc_a, self.pc_b = sid, pc_a, pc_b
        self.sender = sender            # "pc_a" or "pc_b"
        self.swap_requested_by = None

    @property
    def receiver(self):
        return "pc_b" if self.sender == "pc_a" else "pc_a"

    def allowed(self, from_pc, kind):
        # screen frames and file data: sender -> receiver only
        if kind in ("screen", "file_data"):
            return from_pc == self.sender
        # input and file requests: receiver -> sender only
        if kind in ("input", "file_request"):
            return from_pc == self.receiver
        return True

    def request_swap(self, from_pc):
        self.swap_requested_by = from_pc

    def accept_swap(self, from_pc):
        if self.swap_requested_by and self.swap_requested_by != from_pc:
            self.sender = self.receiver
            self.swap_requested_by = None
            return True
        return False