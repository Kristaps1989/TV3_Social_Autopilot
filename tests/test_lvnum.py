"""Kārtas skaitļi ierunā.

Balss «59. minūtē» nolasīja kā «piecdesmit devītā minūtē» — punkts aiz cipara
tai nozīmē kārtas skaitli nominatīvā, bet latviski tur vajag lokatīvu.
Locījumu nosaka nākamais vārds, un to sintēze nedara.
"""
import pytest

from app import lvnum


@pytest.mark.parametrize("raw,spoken", [
    # tas, ko pamanīja redakcija
    ("Vienīgos vārtus 59. minūtē guva Saka.",
     "Vienīgos vārtus piecdesmit devītajā minūtē guva Saka."),
    # lokatīvs pēc dažādām galotnēm
    ("Latvija ir 1. vietā.", "Latvija ir pirmajā vietā."),
    ("2. puslaikā spēle mainījās.", "otrajā puslaikā spēle mainījās."),
    ("Notiks 1. septembrī.", "Notiks pirmajā septembrī."),
    ("21. gadsimtā", "divdesmit pirmajā gadsimtā"),
    ("100. reizē", "simtajā reizē"),
    # gadskaitļi
    ("1998. gadā", "tūkstoš deviņi simti deviņdesmit astotajā gadā"),
    ("2020. gadā", "divi tūkstoši divdesmitajā gadā"),
    # nominatīvs — nākamais vārds ir bez garumzīmes
    ("Viņam pienākas 3. vieta.", "Viņam pienākas trešā vieta."),
    ("Tas bija 2. mēģinājums.", "Tas bija otrais mēģinājums."),
    # akuzatīvs, ģenitīvs, datīvs un daudzskaitlis — tie sporta tekstā ir
    # tikpat bieži kā lokatīvs, un ar «s» galotni tie visi izskatās vienādi
    ("Pakāpjas uz 2. vietu.", "Pakāpjas uz otro vietu."),
    ("1. vietas dēļ.", "pirmās vietas dēļ."),
    ("Balva 1. vietai.", "Balva pirmajai vietai."),
    ("90. gadi bija citi.", "deviņdesmitie gadi bija citi."),
    ("20. gados Latvijā", "divdesmitajos gados Latvijā"),
    ("2. vietās palika abi.", "otrajās vietās palika abi."),
])
def test_ordinals_take_the_case_the_next_word_asks_for(raw, spoken):
    assert lvnum.speak_ordinals(raw) == spoken


@pytest.mark.parametrize("raw", [
    "Rezultāts 1:0, spēle beidzās.",     # nav punkta aiz cipara
    "Cena ir 12.50 eiro.",               # decimāldaļa, nevis kārtas skaitlis
    "Notika 2000. gadā.",                # apaļš gadskaitlis -> neaiztiekam
])
def test_untouched_when_the_case_cannot_be_told(raw):
    """Uzminēts locījums skan sliktāk nekā tas, ko balss dara šodien."""
    assert lvnum.speak_ordinals(raw) == raw


# --- punkts teikuma beigās nav kārtas skaitļa zīme --------------------------

@pytest.mark.parametrize("raw,spoken", [
    # tas, ko pamanīja redakcija: balss teica «sešdesmit ceturtais»
    ("Serbija uzvarēja Itāliju ar rezultātu 76 pret 64.",
     "Serbija uzvarēja Itāliju ar rezultātu 76 pret sešdesmit četri."),
    # arī teikuma vidū, kad aiz punkta sākas nākamais teikums
    ("Rezultāts bija 76 pret 64. Jokičs guva astoņus punktus.",
     "Rezultāts bija 76 pret sešdesmit četri. Jokičs guva astoņus punktus."),
    ("Spēle beidzās 59.", "Spēle beidzās piecdesmit deviņi."),
    ("Cena bija 1250.", "Cena bija tūkstoš divi simti piecdesmit."),
])
def test_a_number_ending_a_sentence_is_read_as_a_plain_number(raw, spoken):
    assert lvnum.speak_ordinals(raw) == spoken


def test_a_lowercase_word_after_the_dot_still_means_an_ordinal():
    """Lielais burts nozīmē jaunu teikumu, mazais — locījumu. Tieši tas
    atšķir «64. Jokičs» no «64. minūtē»."""
    assert lvnum.speak_ordinals("Vārti 64. minūtē.") == \
        "Vārti sešdesmit ceturtajā minūtē."
    assert lvnum.speak_ordinals("Rezultāts 64. Jokičs guva.") == \
        "Rezultāts sešdesmit četri. Jokičs guva."


@pytest.mark.parametrize("n,words", [
    (0, "nulle"), (4, "četri"), (10, "desmit"), (11, "vienpadsmit"),
    (20, "divdesmit"), (64, "sešdesmit četri"), (100, "simts"),
    (250, "divi simti piecdesmit"), (1000, "tūkstotis"),
    (2026, "divi tūkstoši divdesmit seši"),
])
def test_cardinal_numbers(n, words):
    assert lvnum.cardinal(n) == words


def test_only_the_last_part_of_a_compound_number_is_an_ordinal():
    assert lvnum.ordinal(59, "loc") == "piecdesmit devītajā"
    assert lvnum.ordinal(20, "loc") == "divdesmitajā"
    assert lvnum.ordinal(9, "nom_m") == "devītais"
    assert lvnum.ordinal(9, "nom_f") == "devītā"


def test_the_spoken_layer_applies_it_before_the_pronunciation_table():
    from app import tts

    out = tts.spoken_text("Vārti 59. minūtē. Lasi tv3.lv", {})
    assert "piecdesmit devītajā minūtē" in out
    assert "tv trīs punkts lv" in out
    # rakstītais teksts paliek neskarts — priekšskatījumā redaktors grib
    # redzēt to, kas būs uz ekrāna
    assert "59." not in out
