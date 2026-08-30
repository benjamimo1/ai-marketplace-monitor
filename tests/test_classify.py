import pytest

from ai_marketplace_monitor.classify import ACCESSORY, FOR_PARTS, OTHER, classify, is_product


@pytest.mark.parametrize(
    "title,expected",
    [
        # the target model, written the many ways real sellers write it
        ("IPad Air 5th (M1) 64 gb", "iPad Air 5 (M1)"),
        ("iPad Air 5ta generación 64gb", "iPad Air 5 (M1)"),
        ("iPad Air Quinta Generación", "iPad Air 5 (M1)"),
        ("IPad Air M1 (64gb)", "iPad Air 5 (M1)"),
        ("iPad Air 5ª generación – Chip M1 | 64 GB | Wi-Fi", "iPad Air 5 (M1)"),
        ("iPad Air 5 256 GB", "iPad Air 5 (M1)"),
        ("iPad Air 5ta gen", "iPad Air 5 (M1)"),
        # neighbouring generations that must not be pooled with it
        ("iPad Air 4ta generación", "iPad Air 4"),
        ("Ipad Air 4ta 64 GB + Apple pencil 2 + Mica Hoja", "iPad Air 4"),
        ("iPad Air (4.ª generación) 256 GB + Magic Keyboard", "iPad Air 4"),
        ("Apple iPad Air 3ª Generación 64GB Wi-Fi", "iPad Air 3"),
        ("iPad Air 2 9.7 Wifi+4g 64 gb space gray", "iPad Air 2"),
        ("ipad Air (2013) 1ra generación", "iPad Air 1"),
        ("ipad Air M2", "iPad Air M2 (6th)"),
        ("IPAD AIR 6ta generación 32 GB", "iPad Air M2 (6th)"),
        ("Ipad air m4", "iPad Air M4"),
        # base iPads and Pros, which share the search results
        ("iPad 9na generación", "iPad (base)"),
        ("Vendo IPad (décima generación) 64 GB", "iPad (base)"),
        ("Ipad 6 sexta generación 128gb", "iPad (base)"),
        ("Se vende Ipad Pro (11 Pulgadas)", "iPad Pro"),
        # not a device at all
        ("Lápiz iPad", ACCESSORY),
        ("MAGIC KEYBOARD IPAD AIR CUARTA GENERACION", ACCESSORY),
        ("macbook air m1 detalle", OTHER),
        ("Ipad air 4 10.9  para repuesto", FOR_PARTS),
    ],
)
def test_classify(title: str, expected: str) -> None:
    assert classify(title) == expected


def test_a_device_sold_with_extras_is_still_the_device() -> None:
    # the accessory words appear, but the listing sells the tablet
    assert classify("ipad air 5ta generación + apple pencil") == "iPad Air 5 (M1)"
    assert classify("iPad Air 5ta Gen M1 64GB + Apple Pencil 2 + funda") == "iPad Air 5 (M1)"


def test_screen_size_is_not_read_as_a_generation() -> None:
    assert classify("10.9 pulgadas iPad Air 4") == "iPad Air 4"
    assert classify('iPad Air M1 10.9" 256gb (5ta generación)') == "iPad Air 5 (M1)"


def test_unrecognised_titles_are_not_guessed() -> None:
    assert classify("tablet barata") == "unknown"
    assert classify("") == "unknown"


def test_description_breaks_a_tie_the_title_cannot() -> None:
    assert classify("iPad Air") == "unknown"
    assert classify("iPad Air", "iPad Air 5ta generacion chip M1 64GB") == "iPad Air 5 (M1)"


def test_is_product_excludes_non_devices() -> None:
    assert is_product("iPad Air 5 (M1)")
    assert not is_product(ACCESSORY)
    assert not is_product(FOR_PARTS)
    assert not is_product(OTHER)
    assert not is_product("unknown")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("iPad Air 5ta Gen M1 64GB + Apple Pencil 2 + funda", 2),
        ("Vendo mi iPad Air junto con el Apple Pencil de 2da Generación", 2),
        ("iPad Air 11 M2 256 gb + Apple Pencil Pro", 2),
        ("iPad de novena generación + Apple Pencil primera generacion", 1),
        # the tablet's generation sits right beside the pencil and must not be
        # read as the pencil's: mentioned, but generation unstated
        ("ipad air 5ta generación + apple pencil", 0),
        ("iPad Air 5ta Generación color rosado + Apple Pencil", 0),
        # no pencil at all
        ("iPad Air 5ta generación 64gb", None),
    ],
)
def test_pencil_generation(text: str, expected) -> None:
    from ai_marketplace_monitor.classify import pencil

    assert pencil(text) == expected


def test_pencil_generation_found_in_description() -> None:
    from ai_marketplace_monitor.classify import pencil

    # the title says nothing; the description is where the value is
    assert pencil("iPad Air 5ta generación 64gb") is None
    assert pencil("iPad Air 5ta generación 64gb", "Incluye Apple Pencil 2ª generación") == 2


def test_bundle_alert_rule() -> None:
    """A device-plus-pencil bundle under the alert price is a straight buy.

    Encodes the user's rule: an iPad Air 5 (M1) with an Apple Pencil asking
    below 300.000 CLP is worth taking at the asking price. A stated 1st
    generation pencil does not qualify -- it is not worth the same resale.
    """
    from ai_marketplace_monitor.classify import classify, pencil

    def is_steal(title: str, ask: int, limit: int = 300000) -> bool:
        generation = pencil(title)
        return (
            classify(title) == "iPad Air 5 (M1)"
            and generation is not None
            and generation != 1
            and ask < limit
        )

    assert is_steal("iPad Air 5ta generacion M1 + Apple Pencil 2", 280000)
    assert is_steal("iPad Air 5 M1 64GB con apple pencil", 295000)  # unstated still counts
    assert not is_steal("iPad Air 5ta generacion 64gb", 290000)  # no pencil
    assert not is_steal("iPad Air 5 M1 + Apple Pencil 1ra generacion", 280000)  # gen 1
    assert not is_steal("iPad Air 5ta generacion M1 + Apple Pencil 2", 330000)  # over the limit
    assert not is_steal("iPad Air 4ta generacion + Apple Pencil 2", 280000)  # wrong model
