
from reach_bot.ranking import Affinity, rank_candidates


class Fake:
    def public_channels(self, target_id):
        return ["small", "large"]

    def channel_members(self, channel_id):
        return ["target", "a"] if channel_id == "small" else ["target", "a", "b", "c"]

    def thread_cooccurrences(self, target_id, channel_ids, since):
        return {"b": 0.9}

    def presence(self, user_id):
        return "active" if user_id in {"a", "b"} else "offline"

    def affinities(self, target_id):
        return {"b": Affinity(10, 2), "a": Affinity(0.5, 3)}


def test_rank_keeps_presence_buckets_and_threshold():
    result = rank_candidates("target", "requester", Fake(), max_per_bucket=3)
    assert [c.user_id for c in result.active] == ["a", "b"]
    assert all(c.affinity is None or c.user_id == "a" for c in result.active)


def test_rank_caps_bucket():
    class Many(Fake):
        def channel_members(self, channel_id):
            return ["target", "a", "b", "c", "d"]

        def presence(self, user_id):
            return "active"

    result = rank_candidates("target", "requester", Many(), max_per_bucket=2)
    assert len(result.active) == 2
