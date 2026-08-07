"""Verification de la configuration publiee vers la tablette.

Lancer : py test_session.py

Le poste est la seule source de la configuration, mais elle ressort telle
quelle vers la tablette : ce qui passe ici est ce que l'autre ecran va
appliquer sans le rediscuter. Deux choses sont donc verifiees :

1. le fond et le cadrage traversent bien -- c'est la promesse du mode
   tablette, « le trace vu sous le stylet est celui que le poste affiche » ;
2. une valeur aberrante ne traverse pas telle quelle. Un zoom nul ou une
   position hors bornes donnerait une geometrie que le navigateur ne sait pas
   dessiner, et l'ecran de la tablette resterait vide sans rien signaler.
"""

import sys

import session


def fresh():
    return session.Session()


def test_background_reaches_the_tablet():
    got = fresh().set_config({"background": "#808080"})
    assert got["background"] == "#808080", got
    return "fond #808080 publie"


def test_checker_is_a_background_like_any_other():
    got = fresh().set_config({"background": "checker"})
    assert got["background"] == "checker", got
    return "damier publie comme un fond"


def test_media_fit_reaches_the_tablet():
    got = fresh().set_config(
        {"mediaFit": {"mode": "crop", "zoom": 2.5, "posX": 0.25, "posY": 0.75}})
    fit = got["mediaFit"]
    assert fit == {"mode": "crop", "zoom": 2.5, "posX": 0.25, "posY": 0.75}, fit
    return "recadrage zoom 2.5 en (0.25, 0.75)"


def test_defaults_are_a_whole_media_centred():
    fit = fresh().config["mediaFit"]
    assert fit["mode"] == "contain" and fit["zoom"] == 1.0, fit
    assert fit["posX"] == 0.5 and fit["posY"] == 0.5, fit
    return "contain, zoom 1, centre"


def test_an_unknown_mode_falls_back_to_contain():
    # Ne jamais rogner sur une valeur qu'on ne comprend pas : montrer le media
    # entier est le repli qui ne cache rien.
    got = fresh().set_config({"mediaFit": {"mode": "n_importe_quoi"}})
    assert got["mediaFit"]["mode"] == "contain", got["mediaFit"]
    return "mode inconnu -> contain"


def test_zoom_below_one_is_refused():
    # Un zoom < 1 laisserait la region de recadrage deborder du media : le
    # cadre montrerait du vide, que rien ne remplit.
    got = fresh().set_config({"mediaFit": {"mode": "crop", "zoom": 0}})
    assert got["mediaFit"]["zoom"] == 1.0, got["mediaFit"]
    return "zoom 0 ramene a 1"


def test_zoom_is_capped():
    got = fresh().set_config({"mediaFit": {"mode": "crop", "zoom": 99}})
    assert got["mediaFit"]["zoom"] == 4.0, got["mediaFit"]
    return "zoom 99 ramene a 4 (borne du curseur)"


def test_positions_stay_inside_the_media():
    got = fresh().set_config(
        {"mediaFit": {"mode": "crop", "posX": -3, "posY": 42}})
    fit = got["mediaFit"]
    assert fit["posX"] == 0.0 and fit["posY"] == 1.0, fit
    return "positions ramenees dans [0, 1]"


def test_garbage_does_not_crash_the_session():
    # La route est ouverte au reseau local : une charge utile absurde doit
    # laisser la session debout, pas la casser pour les deux ecrans.
    got = fresh().set_config(
        {"mediaFit": {"mode": "crop", "zoom": "beaucoup", "posX": None}})
    fit = got["mediaFit"]
    assert fit["zoom"] == 1.0 and fit["posX"] == 0.5, fit
    return "valeurs illisibles -> defauts"


def test_media_fit_must_be_an_object():
    got = fresh().set_config({"mediaFit": "crop"})
    assert got["mediaFit"]["mode"] == "contain", got["mediaFit"]
    return "chaine ignoree, cadrage inchange"


def test_format_still_travels():
    # La regression qu'on ne veut pas : elargir la whitelist sans casser ce
    # qu'elle laissait deja passer.
    got = fresh().set_config(
        {"width": 3840, "height": 2160, "fps": 24, "alpha": False})
    assert got["width"] == 3840 and got["height"] == 2160, got
    assert got["fps"] == 24 and got["alpha"] is False, got
    return "3840x2160 @ 24, sans alpha"


def test_a_partial_update_leaves_the_rest_alone():
    live = fresh()
    live.set_config({"width": 3840, "height": 2160, "background": "#000000"})
    got = live.set_config({"mediaFit": {"mode": "crop"}})
    assert got["width"] == 3840 and got["background"] == "#000000", got
    assert got["mediaFit"]["mode"] == "crop", got["mediaFit"]
    return "recadrer ne reinitialise ni le format ni le fond"


def test_a_page_number_travels_with_the_media():
    # La tablette charge la meme URL que le poste : elle n'a pas a savoir quelle
    # page c'etait. Mais le poste, lui, en a besoin pour rouvrir le PDF apres un
    # export ou un passage en mode tablette.
    live = fresh()
    got = live.set_media({"id": "abc", "state": "ready", "kind": "image",
                          "name": "rapport.pdf (page 3)", "page": 3, "pageCount": 12})
    assert got["page"] == 3 and got["pageCount"] == 12, got
    assert live.snapshot()["media"]["page"] == 3, live.snapshot()["media"]
    return "page 3 sur 12, publiee avec le media"


def test_a_media_without_pages_says_so():
    # Un rush n'a pas de page : la cle doit rester vide plutot que valoir 1,
    # sinon `_current_page` en inventerait une a chaque reouverture.
    media = fresh().set_media({"id": "def", "state": "ready", "kind": "video",
                               "name": "rush.mov"})
    assert media["page"] is None, media["page"]
    assert media["pageCount"] == 0, media["pageCount"]
    return "page absente sur un rush ordinaire"


def test_the_in_out_points_travel_to_the_tablet():
    """Le coeur du minutage partage.

    Le trace commence au point IN : c'est l'origine commune des deux ecrans.
    Des bornes qui ne traversent pas, et la tablette annote le meme rush sur un
    autre minutage -- rien ne retombe en face a l'export.
    """
    got = fresh().set_config({"inPoint": 12.5, "outPoint": 48.0})
    assert got["inPoint"] == 12.5 and got["outPoint"] == 48.0, got
    return "IN 12.5 s / OUT 48 s publies"


def test_no_media_means_no_bounds_yet():
    config = fresh().config
    assert config["inPoint"] is None and config["outPoint"] is None, config
    return "aucun media -> aucune borne"


def test_an_empty_interval_is_refused():
    # Une sortie avant l'entree ne decrit rien de jouable : le rush resterait
    # fige chez celui qui recoit ces bornes, sans que rien ne l'explique.
    session_ = fresh()
    session_.set_config({"inPoint": 10.0, "outPoint": 20.0})
    got = session_.set_config({"inPoint": 30.0, "outPoint": 5.0})
    assert got["inPoint"] == 10.0 and got["outPoint"] == 20.0, got
    return "OUT avant IN refuse, bornes precedentes gardees"


def test_a_negative_in_point_is_refused():
    session_ = fresh()
    session_.set_config({"inPoint": 3.0, "outPoint": 9.0})
    got = session_.set_config({"inPoint": -4.0, "outPoint": 9.0})
    assert got["inPoint"] == 3.0, got
    return "IN negatif refuse"


def test_garbage_bounds_do_not_erase_the_good_ones():
    session_ = fresh()
    session_.set_config({"inPoint": 1.0, "outPoint": 2.0})
    got = session_.set_config({"inPoint": "debut", "outPoint": None})
    assert got["inPoint"] == 1.0 and got["outPoint"] == 2.0, got
    return "bornes illisibles ignorees"


def test_bounds_survive_a_partial_update():
    # Recadrer ne dit rien du minutage : les bornes ne doivent pas disparaitre
    # au passage.
    session_ = fresh()
    session_.set_config({"inPoint": 5.0, "outPoint": 6.0})
    got = session_.set_config({"background": "#000000"})
    assert got["inPoint"] == 5.0 and got["outPoint"] == 6.0, got
    return "changer le fond ne touche pas aux bornes"


def test_each_publication_is_visible_as_a_new_revision():
    # La tablette ne relit l'etat que sur changement de revision : sans ce
    # bump, un recadrage ne lui parviendrait jamais.
    live = fresh()
    before = live.revision
    live.set_config({"mediaFit": {"mode": "crop"}})
    assert live.revision > before, (before, live.revision)
    return "revision %d -> %d" % (before, live.revision)


TESTS = [
    test_background_reaches_the_tablet,
    test_checker_is_a_background_like_any_other,
    test_media_fit_reaches_the_tablet,
    test_defaults_are_a_whole_media_centred,
    test_an_unknown_mode_falls_back_to_contain,
    test_zoom_below_one_is_refused,
    test_zoom_is_capped,
    test_positions_stay_inside_the_media,
    test_garbage_does_not_crash_the_session,
    test_media_fit_must_be_an_object,
    test_format_still_travels,
    test_a_partial_update_leaves_the_rest_alone,
    test_a_page_number_travels_with_the_media,
    test_a_media_without_pages_says_so,
    test_the_in_out_points_travel_to_the_tablet,
    test_no_media_means_no_bounds_yet,
    test_an_empty_interval_is_refused,
    test_a_negative_in_point_is_refused,
    test_garbage_bounds_do_not_erase_the_good_ones,
    test_bounds_survive_a_partial_update,
    test_each_publication_is_visible_as_a_new_revision,
]


def main():
    print("Configuration publiee vers la tablette")
    failures = 0
    for test in TESTS:
        try:
            detail = test()
            print("  OK   %-46s %s" % (test.__name__, detail or ""))
        except Exception as exc:  # noqa: BLE001 - rapport de test
            failures += 1
            print("  FAIL %-46s %s: %s" % (test.__name__, type(exc).__name__, exc))
    print("\n%d/%d tests passes" % (len(TESTS) - failures, len(TESTS)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
