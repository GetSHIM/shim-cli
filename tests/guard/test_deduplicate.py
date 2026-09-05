import random

from shim_guard.guard.recognizers import Match, deduplicate


def test_index_preserves_reference_order_and_containment():
    randomizer = random.Random(17)
    for _ in range(50):
        matches = [
            Match(
                randomizer.choice(["EMAIL", "SECRET"]),
                start,
                start + randomizer.randrange(1, 40),
                randomizer.choice([0.2, 0.5, 1.0]),
            )
            for start in (randomizer.randrange(100) for _ in range(200))
        ]
        ordered = sorted(
            set(matches),
            key=lambda item: (
                -item.score,
                item.start,
                -(item.end - item.start),
                item.entity_type,
            ),
        )
        expected = []
        for item in ordered:
            if not any(
                other.entity_type == item.entity_type
                and other.start <= item.start
                and other.end >= item.end
                for other in expected
            ):
                expected.append(item)
        assert deduplicate(matches) == expected
    dense = [Match("SECRET", i * 2, i * 2 + 1, 1.0) for i in range(10_000)]
    assert deduplicate(dense) == dense
