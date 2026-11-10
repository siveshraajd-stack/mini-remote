class RoleManager:
    def __init__(self, capture, input_rules, transfer):
        self.capture, self.input_rules, self.transfer = capture, input_rules, transfer
        self.mode = None

    def apply(self, mode):            # "sender" or "receiver"
        if mode == self.mode:
            return
        self.stop_all()
        if mode == "sender":
            self.capture.start()
            self.input_rules.enable()
        else:
            self.transfer.cancel_pending()
        self.mode = mode

    def stop_all(self):
        self.capture.stop()
        self.input_rules.disable()
        self.transfer.cancel_pending()
        close_open_popups()