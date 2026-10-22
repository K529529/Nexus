import unittest

from group_events import group_events


class GroupEventsTest(unittest.TestCase):
    def test_groups_by_user_and_preserves_all_fields(self):
        events = [
            {"user": "alice", "at": "2024-05-02T10:00:00Z", "action": "login", "meta": {"ip": "1.1.1.1"}},
            {"user": "bob", "at": "2024-05-01T09:00:00Z", "action": "logout"},
            {"user": "alice", "at": "2024-05-01T08:00:00Z", "action": "view"},
        ]

        grouped = group_events(events)

        self.assertEqual(set(grouped), {"alice", "bob"})
        self.assertEqual(grouped["alice"], [events[0], events[2]])
        self.assertEqual(grouped["bob"], [events[1]])
        self.assertEqual(grouped["alice"][0]["meta"], {"ip": "1.1.1.1"})

    def test_input_order_kept_within_group(self):
        events = [
            {"user": "u", "at": "2024-05-03T00:00:00Z"},
            {"user": "u", "at": "2024-05-01T00:00:00Z"},
            {"user": "u", "at": "2024-05-02T00:00:00Z"},
        ]

        self.assertEqual(group_events(events)["u"], events)

    def test_does_not_alias_input_events(self):
        event = {"user": "u", "at": "2024-05-01T00:00:00Z"}
        grouped = group_events([event])
        grouped["u"][0]["extra"] = True
        self.assertEqual(event, {"user": "u", "at": "2024-05-01T00:00:00Z"})

    def test_empty_input(self):
        self.assertEqual(group_events([]), {})


if __name__ == "__main__":
    unittest.main()
