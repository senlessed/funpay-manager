class WakeGroup:
    def __init__(self, *events):
        self.events = events

    def set(self):
        for event in self.events:
            event.set()
