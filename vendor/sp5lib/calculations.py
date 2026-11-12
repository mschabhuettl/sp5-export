"""Zentrale Berechnungsschicht für Schichtplaner5 (Original-Spec, Kapitel 3).

Reine Funktionen über einfache dict-Datensätze, exakt wie
:func:`sp5lib.dbf_reader.read_dbf` sie liefert — kein I/O, keine
SP5Database-Abhängigkeit. Die MA-Berechnungsparameter (5EMPL) reisen als
leichtgewichtiger :class:`EmployeeContext`; der Feiertagskalender (5HOLID) als
einfaches ``date -> INTERVAL``-Mapping aus :func:`holiday_calendar`. Alle an
die Funktionen übergebenen Datensatz-Sammlungen sind auf EINEN Mitarbeiter
vorgefiltert (5MASHI/5SPSHI/5ABSEN/5BOOK/5OVER/5LEAEN dieses MA);
Schichtdefinitionen kommen als ``shifts_by_id``.

Konventionen (Spec 3.1):

- Wochentag-Index ``0 = Montag .. 6 = Sonntag``; Tag-Index ``7`` ist der
  Feiertags-Slot („Ft") für ``WORKDAYS``/``STARTEND``/``DURATION``/Bedarfstabellen.
- Uhrzeiten sind Minuten seit Mitternacht (ganzer Tag 1440, Halbtagsgrenze 720).
- Stundenwerte sind Floats (Minuten / 60); Float-Null-Vergleiche nutzen ``EPSILON``.
- jede MA-bezogene Zeitraumsumme klemmt ``[von, bis]`` auf den
  Beschäftigungszeitraum ``EMPSTART``/``EMPEND`` (ungesetzte Grenzen bleiben
  offen); 5BOOK-Kontobuchungen werden VOR dem Klemmen summiert und wirken
  allein über ihr ``DATE``.
"""

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

Record = dict[str, Any]
# 5HOLID-Kalender: DATE -> INTERVAL (0 = ganztägiger Feiertag, 1 = erste
# Hälfte 0..720 min, 2 = zweite Hälfte 720..1440 min).
Holidays = Mapping[date, int]

EPSILON = 1e-6  # float zero comparisons (spec 3.1 no. 7)
DAY_MINUTES = 1440
HALF_DAY_MINUTES = 720  # half-day boundary 12:00 (spec 3.1 no. 5)
HOLIDAY_INDEX = 7  # day index of the "Ft" slot (spec 3.1 no. 4)

# 5BOOK.TYPE account codes (spec 3.6.1)
BOOKING_ACTUAL = 0
BOOKING_NOMINAL = 1
BOOKING_OVERTIME = 2


# ── Basic conversions (spec 3.1, 2.3) ───────────────────────────


def to_date(value: Any) -> date | None:
    """Coerce a record date value (ISO string from read_dbf or date) to date."""
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def parse_day_mask(mask: str, slots: int) -> tuple[bool, ...]:
    """Parse a weekday flag mask (D-35/D-36).

    The original stores flags **space-separated**, e.g. ``"1 1 1 1 1 0 0 0"``.
    A compact form without separators (e.g. ``"1111111"``) is also accepted so
    masks written by older clients still parse correctly — otherwise ``split()``
    would see a single token and only the first weekday would ever be active.

    ``slots`` is 8 for WORKDAYS/5LEAVT.VALIDDAYS (Mon..Sun + Ft) and 7 for
    5XCHAR.VALIDDAYS (Mon..Sun without the Ft slot). Missing slots are False.
    """
    tokens = (mask or "").split()
    if len(tokens) <= 1:
        # Kompaktform ("1111111") — jedes Zeichen als ein Flag behandeln.
        tokens = list(tokens[0]) if tokens else []
    return tuple(
        bool(tokens[i]) and tokens[i] != "0" if i < len(tokens) else False
        for i in range(slots)
    )


def normalize_day_mask(mask: str, slots: int) -> str:
    """Gibt eine Wochentagsmaske in der kanonischen leerzeichengetrennten Form
    des Originals aus (z. B. ``"1 1 1 1 1 1 1"``); akzeptiert kompakte wie
    getrennte Eingabe. Beim Schreiben genutzt, damit gespeicherte Masken
    byte-paritätisch zum Original-Layout bleiben."""
    flags = parse_day_mask(mask, slots)
    return " ".join("1" if f else "0" for f in flags)


def _parse_minutes(token: str) -> int | None:
    """Parst ``"HH:MM"`` zu Minuten seit Mitternacht (D-28/D-29)."""
    hours, sep, minutes = token.partition(":")
    if not sep:
        return None
    try:
        return int(hours) * 60 + int(minutes)
    except ValueError:
        return None


def parse_startend(value: str) -> list[tuple[int, int]]:
    """Parse a STARTEND work-time string (D-31) into (start, end) minute pairs.

    Format: up to three space-separated ``HH:MM-HH:MM`` intervals; an empty
    string means "not defined on this day index". End <= start means the
    interval crosses midnight (D-30).
    """
    windows = []
    for token in (value or "").split():
        start_s, sep, end_s = token.partition("-")
        if not sep:
            continue
        start = _parse_minutes(start_s)
        end = _parse_minutes(end_s)
        if start is not None and end is not None:
            windows.append((start, end))
    return windows


def holiday_calendar(holid_records: Iterable[Record]) -> dict[date, int]:
    """Baut das ``date -> INTERVAL``-Feiertags-Mapping aus 5HOLID-Sätzen."""
    calendar: dict[date, int] = {}
    for rec in holid_records:
        d = to_date(rec.get("DATE"))
        if d is not None:
            calendar[d] = int(rec.get("INTERVAL") or 0)
    return calendar


def day_index(d: date, holidays: Holidays) -> int:
    """Tag-Index für STARTEND/DURATION/Bedarfs-Lookups: 7 an Feiertagen (3.4.3 Nr. 5)."""
    return HOLIDAY_INDEX if d in holidays else d.weekday()


def _dated_entries(
    entries: Iterable[Record], von: date, bis: date
) -> Iterator[tuple[date, Record]]:
    """Liefert (date, record) für Sätze, deren DATE in [von, bis] liegt."""
    for rec in entries:
        d = to_date(rec.get("DATE"))
        if d is not None and von <= d <= bis:
            yield d, rec


# ── Employee context (5EMPL calculation parameters) ─────────────


@dataclass(frozen=True)
class EmployeeContext:
    """Calculation-relevant 5EMPL parameters (spec 3.1/3.3.1)."""

    workdays: tuple[bool, ...]  # 8 flags Mon..Sun + Ft (WORKDAYS, D-35)
    calcbase: int = 0  # 0=day, 1=week, 2=month, 3=total
    hrs_day: float = 0.0
    hrs_week: float = 0.0
    hrs_month: float = 0.0
    hrs_total: float = 0.0
    deduct_hol: bool = False  # DEDUCTHOL, wirkt nur bei calcbase 1/2
    emp_start: date | None = None  # EMPSTART; None = open (serial 0/1 unset)
    emp_end: date | None = None  # EMPEND; None = open

    @classmethod
    def from_record(cls, rec: Record) -> "EmployeeContext":
        return cls(
            workdays=parse_day_mask(str(rec.get("WORKDAYS") or ""), 8),
            calcbase=int(rec.get("CALCBASE") or 0),
            hrs_day=float(rec.get("HRSDAY") or 0.0),
            hrs_week=float(rec.get("HRSWEEK") or 0.0),
            hrs_month=float(rec.get("HRSMONTH") or 0.0),
            hrs_total=float(rec.get("HRSTOTAL") or 0.0),
            deduct_hol=bool(rec.get("DEDUCTHOL")),
            emp_start=to_date(rec.get("EMPSTART")),
            emp_end=to_date(rec.get("EMPEND")),
        )


def clamp_to_employment(
    emp: EmployeeContext, von: date, bis: date
) -> tuple[date, date]:
    """Klemmt einen Auswertungszeitraum auf den Beschäftigungszeitraum (Spec 3.1 Nr. 8).

    Ungesetzte Grenzen klemmen nicht; das Ergebnis kann leer sein (von > bis).
    """
    if emp.emp_start is not None:
        von = max(von, emp.emp_start)
    if emp.emp_end is not None:
        bis = min(bis, emp.emp_end)
    return von, bis


def is_employed(emp: EmployeeContext, d: date) -> bool:
    """EMPSTART <= d <= EMPEND, ungesetzte Grenzen offen."""
    if emp.emp_start is not None and d < emp.emp_start:
        return False
    if emp.emp_end is not None and d > emp.emp_end:
        return False
    return True


# ── Ersatzsuche / Notfallplan (Eignung eines Vertretungs-MA) ────


def is_restricted(
    restrictions: Iterable[Record], shift_id: int, weekday_index: int
) -> bool:
    """True wenn 5RESTR den MA an *weekday_index* für *shift_id* sperrt.

    ``restrictions`` sind die 5RESTR-Sätze EINES Mitarbeiters. ``weekday_index``
    ist der Tagesindex 0..7 (Mo..So, 7 = Feiertag, D-34) — auf Feiertagen wird
    auch eine Sperre des konkreten Wochentags geprüft, damit eine Mo-Sperre an
    einem Feiertags-Montag nicht stillschweigend entfällt. Ein vorhandener Satz
    bedeutet "gesperrt" (RESTRICT, D-71: kleines Enum, Sperre = Satz existiert).
    """
    for r in restrictions:
        if int(r.get("SHIFTID") or 0) != shift_id:
            continue
        wd = int(r.get("WEEKDAY") or 0)
        if wd == weekday_index or (weekday_index == HOLIDAY_INDEX and wd == HOLIDAY_INDEX):
            return True
    return False


def is_eligible_replacement(
    emp: EmployeeContext,
    d: date,
    shift_id: int,
    holidays: Holidays,
    *,
    is_hidden: bool,
    in_group: bool,
    busy_dates: Iterable[date],
    absent_dates: Iterable[date],
    restrictions: Iterable[Record],
) -> tuple[bool, str | None]:
    """Eignung eines Mitarbeiters als Vertretung für *shift_id* am Tag *d*.

    Harte Kriterien (Original Kap. 5/6, Datenmodell 5RESTR/5GRASG/5EMPL):

    1. nicht ausgeblendet (5EMPL.HIDE),
    2. im Beschäftigungszeitraum (EMPSTART/EMPEND, D-? / 3.1),
    3. zugehörig zum betrachteten Bereich/zur Gruppe (5GRASG),
    4. verfügbar: an dem Tag weder schon eingeteilt (5MASHI/5SPSHI) noch
       abwesend (5ABSEN),
    5. schicht-/dienstkompatibel: keine 5RESTR-Sperre für (Wochentag, Schicht).

    Liefert ``(True, None)`` bei Eignung, sonst ``(False, grund)`` mit einem
    kurzen deutschen Ausschlussgrund (erster zutreffender Grund).
    """
    if is_hidden:
        return False, "ausgeblendet"
    if not is_employed(emp, d):
        return False, "nicht im Beschäftigungszeitraum"
    if not in_group:
        return False, "nicht im Bereich"
    if d in set(absent_dates):
        return False, "abwesend"
    if d in set(busy_dates):
        return False, "bereits eingeteilt"
    if is_restricted(restrictions, shift_id, day_index(d, holidays)):
        return False, "Schichtrestriktion (5RESTR)"
    return True, None


# ── 3.2 Arbeitstage und Feiertage ───────────────────────────────


def count_holidays_on_workdays(
    emp: EmployeeContext, von: date, bis: date, holidays: Holidays
) -> float:
    """Spec 3.2.2 no. 7: holidays in [von, bis] falling on a marked workday.

    Full holidays (INTERVAL = 0) weigh 1.0, half holidays 0.5. The caller is
    responsible for the WORKDAYS[7] gate (3.2.2 no. 6).
    """
    total = 0.0
    for d, interval in holidays.items():
        if von <= d <= bis and emp.workdays[d.weekday()]:
            total += 1.0 if interval == 0 else 0.5
    return total


def count_working_days(
    emp: EmployeeContext, von: date, bis: date, holidays: Holidays
) -> float:
    """Spec 3.2.2 no. 6: working days in [von, bis], clamped to employment."""
    von, bis = clamp_to_employment(emp, von, bis)
    if von > bis:
        return 0.0
    n = (bis - von).days + 1
    cnt = [n // 7] * 7  # whole weeks
    for i in range(n % 7):  # distribute remainder days
        cnt[(von.weekday() + i) % 7] += 1
    for wd in range(7):
        if not emp.workdays[wd]:
            cnt[wd] = 0
    if emp.workdays[HOLIDAY_INDEX]:
        ft_deduction = 0.0  # holidays ARE workdays
    else:
        ft_deduction = count_holidays_on_workdays(emp, von, bis, holidays)
    return sum(cnt) - ft_deduction


def is_working_day(emp: EmployeeContext, d: date, holidays: Holidays) -> bool:
    """Spec 3.2.2 Nr. 9: halbe Feiertage machen den einzelnen Tag NICHT frei."""
    if not is_employed(emp, d):
        return False
    if not emp.workdays[d.weekday()]:
        return False
    if not emp.workdays[HOLIDAY_INDEX]:
        interval = holidays.get(d)
        if interval is not None and interval == 0:
            return False
    return True


# ── 3.6.1 Account bookings (needed by 3.3/3.4/3.6) ──────────────


def booking_sum(
    bookings: Iterable[Record], account: int, von: date, bis: date
) -> float:
    """Summiert 5BOOK-Werte eines Kontos (TYPE) mit DATE in [von, bis].

    Spec 3.6.1 Nr. 2: Buchungen wirken allein über ihr DATE — kein
    Beschäftigungs-Klemmen (die Aufrufer summieren vor dem Klemmen).
    """
    total = 0.0
    for _d, rec in _dated_entries(bookings, von, bis):
        if int(rec.get("TYPE") or 0) == account:
            total += float(rec.get("VALUE") or 0.0)
    return total


# ── 3.3 Nominal hours ───────────────────────────────────────────


def _nominal_week(
    emp: EmployeeContext, von: date, bis: date, holidays: Holidays
) -> float:
    """Spec 3.3.3 no. 7 (CALCBASE = 1): calendar-fixed Mon-Sun weeks."""
    if (bis - von).days + 1 < 7:
        return count_working_days(emp, von, bis, holidays) * emp.hrs_day
    wd_v = von.weekday()
    wd_b = bis.weekday()
    rest = 0.0
    if wd_v != 0:  # head rest week up to Sunday
        rest += count_working_days(emp, von, von + timedelta(days=6 - wd_v), holidays)
        von = von + timedelta(days=7 - wd_v)  # next Monday
    if wd_b < 6:  # tail rest week from Monday
        rest += count_working_days(emp, bis - timedelta(days=wd_b), bis, holidays)
        bis = bis - timedelta(days=wd_b + 1)  # previous Sunday
    if emp.deduct_hol and von <= bis:  # nur der Ganze-Wochen-Bereich
        rest -= count_holidays_on_workdays(emp, von, bis, holidays)
    full_weeks = max(0, (bis - von).days + 1) // 7
    return rest * emp.hrs_day + full_weeks * emp.hrs_week


def _last_of_month(d: date) -> date:
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def _nominal_month(
    emp: EmployeeContext, von: date, bis: date, holidays: Holidays
) -> float:
    """Spec 3.3.4 no. 9 (CALCBASE = 2): decomposition at calendar months."""
    same_month = (von.year, von.month) == (bis.year, bis.month)
    full_month = von.day == 1 and bis == _last_of_month(bis)
    if same_month and not full_month:
        return count_working_days(emp, von, bis, holidays) * emp.hrs_day
    rest = 0.0
    if von.day != 1:
        rest += count_working_days(emp, von, _last_of_month(von), holidays)
        von = _last_of_month(von) + timedelta(days=1)  # 1. des Folgemonats
    if bis != _last_of_month(bis):
        rest += count_working_days(emp, date(bis.year, bis.month, 1), bis, holidays)
        bis = date(bis.year, bis.month, 1) - timedelta(days=1)  # Ende des Vormonats
    full_months = 12 * (bis.year - von.year) + bis.month - von.month + 1
    if emp.deduct_hol and von <= bis:  # nur der Ganze-Monate-Bereich
        rest -= count_holidays_on_workdays(emp, von, bis, holidays)
    return rest * emp.hrs_day + max(0, full_months) * emp.hrs_month


def _nominal_total(
    emp: EmployeeContext, von: date, bis: date, holidays: Holidays
) -> float:
    """Spec 3.3.5 no. 12 (CALCBASE = 3): HRSTOTAL proportional to workdays."""
    if emp.hrs_total <= EPSILON:
        return 0.0
    if emp.emp_start is None or emp.emp_end is None:
        return 0.0  # invalid configuration (3.3.1 no. 3 requires closed period)
    if von == emp.emp_start and bis == emp.emp_end:
        return emp.hrs_total
    part = count_working_days(emp, von, bis, holidays)
    full = count_working_days(emp, emp.emp_start, emp.emp_end, holidays)
    if part <= EPSILON or full <= EPSILON:
        return 0.0
    return emp.hrs_total * part / full


def get_nominal_hours(
    emp: EmployeeContext,
    von: date,
    bis: date,
    *,
    holidays: Holidays,
    bookings: Iterable[Record] = (),
) -> float:
    """Spec 3.3.2 no. 4: nominal hours = TYPE-1 bookings + CALCBASE formula."""
    soll = booking_sum(bookings, BOOKING_NOMINAL, von, bis)
    von, bis = clamp_to_employment(emp, von, bis)
    if von > bis:
        return soll
    if emp.calcbase == 1:
        return soll + _nominal_week(emp, von, bis, holidays)
    if emp.calcbase == 2:
        return soll + _nominal_month(emp, von, bis, holidays)
    if emp.calcbase == 3:
        return soll + _nominal_total(emp, von, bis, holidays)
    return soll + count_working_days(emp, von, bis, holidays) * emp.hrs_day


# ── 3.4 Actual hours / work hours ───────────────────────────────


def shift_hours_on_day(shift: Record, d: date, holidays: Holidays) -> float:
    """Spec 3.4.3 Nr. 5/6: DURATION[idx] mit idx = Feiertag ? 7 : Wochentag.

    Die Dauer zählt nur, wenn für diesen Tag-Index Arbeitszeiten definiert
    sind (STARTEND{idx} hat ein Fenster mit Minuten ≠ 0); sonst 0.
    """
    idx = day_index(d, holidays)
    windows = parse_startend(str(shift.get(f"STARTEND{idx}") or ""))
    if not any(w != (0, 0) for w in windows):
        return 0.0
    return float(shift.get(f"DURATION{idx}") or 0.0)


def _replaced_days(special: list[tuple[date, Record]]) -> set[date]:
    """Tage, deren Normaldienst durch eine Arbeitszeitabweichung ersetzt ist.

    Spec 3.4.4 Nr. 12: ein 5SPSHI-Satz mit gesetzter SHIFTID ist eine
    Arbeitszeitabweichung und ersetzt den Normaldienst des Tages (keine
    Doppelzählung).
    """
    return {d for d, rec in special if int(rec.get("SHIFTID") or 0)}


def get_work_hours(
    emp: EmployeeContext,
    von: date,
    bis: date,
    *,
    holidays: Holidays,
    shifts_by_id: Mapping[int, Record],
    manual_shifts: Iterable[Record] = (),
    cycle_shifts: Iterable[Record] = (),
    special_shifts: Iterable[Record] = (),
) -> float:
    """Spec 3.4.2 no. 2: work time from 5MASHI + expanded 5CYASS + 5SPSHI.

    ``cycle_shifts`` are day entries produced by :func:`expand_cycle_assignments`.
    5OVER entries are NOT part of the work time (3.4.2 no. 3).
    """
    von, bis = clamp_to_employment(emp, von, bis)
    if von > bis:
        return 0.0
    special = list(_dated_entries(special_shifts, von, bis))
    replaced = _replaced_days(special)
    total = 0.0
    for source in (manual_shifts, cycle_shifts):
        for d, rec in _dated_entries(source, von, bis):
            if d in replaced:
                continue
            shift = shifts_by_id.get(int(rec.get("SHIFTID") or 0))
            if shift is not None:
                total += shift_hours_on_day(shift, d, holidays)
    for _d, rec in special:  # 5SPSHI carries its own hours (3.4.4 no. 10)
        total += float(rec.get("DURATION") or 0.0)
    return total


def duty_day_counts(
    emp: EmployeeContext,
    von: date,
    bis: date,
    *,
    holidays: Holidays,
    manual_shifts: Iterable[Record] = (),
    cycle_shifts: Iterable[Record] = (),
    special_shifts: Iterable[Record] = (),
) -> list[int]:
    """Spec 3.4.5 Nr. 14/15: je Tag-Index die Tage mit mindestens einem Dienst.

    Liefert ``cnt[0..7]``; ein Dienst an einem Sonntag, der zugleich Feiertag
    ist, erhöht ``cnt[6]`` UND ``cnt[7]``.
    """
    von, bis = clamp_to_employment(emp, von, bis)
    counts = [0] * 8
    if von > bis:
        return counts
    days = {
        d
        for source in (manual_shifts, cycle_shifts, special_shifts)
        for d, _rec in _dated_entries(source, von, bis)
    }
    for d in days:
        counts[d.weekday()] += 1
        if d in holidays:
            counts[HOLIDAY_INDEX] += 1
    return counts


def get_actual_hours(
    emp: EmployeeContext,
    von: date,
    bis: date,
    *,
    holidays: Holidays,
    shifts_by_id: Mapping[int, Record],
    manual_shifts: Iterable[Record] = (),
    cycle_shifts: Iterable[Record] = (),
    special_shifts: Iterable[Record] = (),
    absences: Iterable[Record] = (),
    leave_types_by_id: Mapping[int, Record] | None = None,
    bookings: Iterable[Record] = (),
) -> float:
    """Spec 3.4.1 no. 1: actual hours composition.

    TYPE-0 bookings + work hours + charged absence (types without DEDUCTACT)
    − charged absence of DEDUCTACT types. Absences never touch the nominal
    hours (3.3.2 no. 6); 5OVER never touches the actual hours (3.6.3 no. 8).
    """
    ist = booking_sum(bookings, BOOKING_ACTUAL, von, bis)
    cvon, cbis = clamp_to_employment(emp, von, bis)
    if cvon > cbis:
        return ist
    ist += get_work_hours(
        emp,
        cvon,
        cbis,
        holidays=holidays,
        shifts_by_id=shifts_by_id,
        manual_shifts=manual_shifts,
        cycle_shifts=cycle_shifts,
        special_shifts=special_shifts,
    )
    sums = absence_sums(
        emp,
        cvon,
        cbis,
        holidays=holidays,
        absences=absences,
        leave_types_by_id=leave_types_by_id or {},
    )
    return ist + sums.charged - sums.charged_deduct_actual


# ── 3.5 Abwesenheiten: Stundenwert und Anrechnung ───────────────


def absence_hours(
    emp: EmployeeContext, absence: Record, leave_type: Record, holidays: Holidays
) -> float:
    """Spec 3.5.2 Nr. 3: Roh-Stundenwert einer Abwesenheit an ihrem Tag."""
    d = to_date(absence.get("DATE"))
    if d is None:
        return 0.0
    interval = int(absence.get("INTERVAL") or 0)
    if not leave_type.get("COUNTALL"):
        if not emp.workdays[d.weekday()]:
            return 0.0
        hol = holidays.get(d)
        if hol is not None:
            if hol == 0:  # full holiday
                if not emp.workdays[HOLIDAY_INDEX]:
                    return 0.0
            elif not emp.workdays[HOLIDAY_INDEX]:
                # Halber Feiertag an freiem Ft: die Abwesenheit auf die
                # Nicht-Feiertagshälfte kappen (Feiertagshälfte: 1 -> 0..720, 2 -> 720..1440).
                free = (
                    (HALF_DAY_MINUTES, DAY_MINUTES) if hol == 1 else (0, HALF_DAY_MINUTES)
                )
                if interval == 0:  # ganztägige Abwesenheit -> die freie Hälfte
                    return emp.hrs_day * 0.5
                if interval in (1, 2):
                    half = (
                        (0, HALF_DAY_MINUTES) if interval == 1 else (HALF_DAY_MINUTES, DAY_MINUTES)
                    )
                    return emp.hrs_day * 0.5 if half == free else 0.0
                if interval == 3:
                    start = int(absence.get("START") or 0)
                    end = int(absence.get("END") or 0)
                    if end <= start:
                        end += DAY_MINUTES
                    minutes = max(0, min(end, free[1]) - max(start, free[0]))
                    return minutes / 60.0
    if interval in (1, 2):
        return emp.hrs_day * 0.5
    if interval == 3:
        minutes = int(absence.get("END") or 0) - int(absence.get("START") or 0)
        if minutes <= 0:
            minutes += DAY_MINUTES  # computational day change
        return minutes / 60.0
    return emp.hrs_day  # INTERVAL = 0, full day


def charge_factor(leave_type: Record, hrs_day: float) -> float:
    """Spec 3.5.3 no. 4: charging factor by CHARGETYP (0 = none, 1 = time,
    2 = fixed hours per day)."""
    chargetyp = int(leave_type.get("CHARGETYP") or 0)
    if chargetyp == 1:
        return 1.0
    if chargetyp == 2:
        if hrs_day <= EPSILON:
            return 0.0
        return float(leave_type.get("CHARGEHRS") or 0.0) / hrs_day
    return 0.0


@dataclass(frozen=True)
class AbsenceSums:
    """Spec 3.5.4 Nr. 5: die drei Stundensummen über einen Zeitraum."""

    charged: float  # Summe 1: angerechnete Stunden der Arten OHNE DEDUCTACT (bezahlt)
    charged_deduct_actual: float  # Summe 2: angerechnete Stunden der DEDUCTACT-Arten
    raw_deduct_overtime: float  # Summe 3: Rohstunden der DEDUCTOVT-Arten


def absence_sums(
    emp: EmployeeContext,
    von: date,
    bis: date,
    *,
    holidays: Holidays,
    absences: Iterable[Record],
    leave_types_by_id: Mapping[int, Record],
) -> AbsenceSums:
    """Zeitraumsummen der Abwesenheits-Anrechnung, beschäftigungsgeklemmt (3.5.4)."""
    von, bis = clamp_to_employment(emp, von, bis)
    charged = deduct_actual = raw_overtime = 0.0
    if von <= bis:
        for _d, rec in _dated_entries(absences, von, bis):
            lt = leave_types_by_id.get(int(rec.get("LEAVETYPID") or 0))
            if lt is None:
                continue
            hours = absence_hours(emp, rec, lt, holidays)
            value = hours * charge_factor(lt, emp.hrs_day)
            if lt.get("DEDUCTACT"):
                deduct_actual += value
            else:
                charged += value
            if lt.get("DEDUCTOVT"):
                raw_overtime += hours
    return AbsenceSums(charged, deduct_actual, raw_overtime)


def absence_days_by_type(
    emp: EmployeeContext,
    von: date,
    bis: date,
    *,
    holidays: Holidays,
    absences: Iterable[Record],
    leave_types_by_id: Mapping[int, Record],
) -> dict[int, float]:
    """Spec 3.5.4 no. 5 (sum 4) / 3.9.3 no. 5: absence days per leave type.

    Days = charged hours of the type / HRSDAY (half days count 0.5).
    """
    von, bis = clamp_to_employment(emp, von, bis)
    days: dict[int, float] = {}
    if von > bis or emp.hrs_day <= EPSILON:
        return days
    for _d, rec in _dated_entries(absences, von, bis):
        lt_id = int(rec.get("LEAVETYPID") or 0)
        lt = leave_types_by_id.get(lt_id)
        if lt is None:
            continue
        hours = absence_hours(emp, rec, lt, holidays) * charge_factor(lt, emp.hrs_day)
        days[lt_id] = days.get(lt_id, 0.0) + hours / emp.hrs_day
    return days


# ── 3.6 Accounts, saldo, overtime ───────────────────────────────


def get_saldo(
    emp: EmployeeContext,
    von: date,
    bis: date,
    *,
    holidays: Holidays,
    shifts_by_id: Mapping[int, Record],
    manual_shifts: Iterable[Record] = (),
    cycle_shifts: Iterable[Record] = (),
    special_shifts: Iterable[Record] = (),
    absences: Iterable[Record] = (),
    leave_types_by_id: Mapping[int, Record] | None = None,
    bookings: Iterable[Record] = (),
) -> float:
    """Spec 3.6.2 Nr. 4: Saldo = Ist − Soll über denselben Zeitraum.

    Es gibt keinen persistierten Kontostand und keinen Jahresabschluss für
    Stundenkonten (3.6.2 Nr. 5/6) — Korrekturen sind ausschließlich 5BOOK-Buchungen.
    """
    ist = get_actual_hours(
        emp,
        von,
        bis,
        holidays=holidays,
        shifts_by_id=shifts_by_id,
        manual_shifts=manual_shifts,
        cycle_shifts=cycle_shifts,
        special_shifts=special_shifts,
        absences=absences,
        leave_types_by_id=leave_types_by_id,
        bookings=bookings,
    )
    soll = get_nominal_hours(emp, von, bis, holidays=holidays, bookings=bookings)
    return ist - soll


def get_overtime_hours(
    emp: EmployeeContext,
    von: date,
    bis: date,
    *,
    holidays: Holidays,
    bookings: Iterable[Record] = (),
    overtimes: Iterable[Record] = (),
    absences: Iterable[Record] = (),
    leave_types_by_id: Mapping[int, Record] | None = None,
) -> float:
    """Spec 3.6.3 no. 7: overtime account (5OVER, Win-Fehlzeiten).

    TYPE-2 bookings + 5OVER hours − raw absence hours of DEDUCTOVT types.
    5OVER entries never flow into actual hours or work time (no. 8).
    """
    total = booking_sum(bookings, BOOKING_OVERTIME, von, bis)
    von, bis = clamp_to_employment(emp, von, bis)
    if von > bis:
        return total
    for _d, rec in _dated_entries(overtimes, von, bis):
        total += float(rec.get("HOURS") or 0.0)
    total -= absence_sums(
        emp,
        von,
        bis,
        holidays=holidays,
        absences=absences,
        leave_types_by_id=leave_types_by_id or {},
    ).raw_deduct_overtime
    return total


# ── 3.7 Leave entitlements (5LEAEN) ─────────────────────────────


def leave_taken(
    emp: EmployeeContext,
    leave_type: Record,
    von: date,
    bis: date,
    *,
    holidays: Holidays,
    absences: Iterable[Record],
    in_days: bool = True,
) -> float:
    """Spec 3.7.1 Nr. 4: Verbrauch einer Abwesenheitsart über [von, bis].

    Der Beitrag je Eintrag folgt 3.5.2/3.5.3; Ergebnis in Tagen (angerechnete
    Stunden / HRSDAY) oder mit ``in_days=False`` (INDAYS = 0) direkt in Stunden.
    """
    von, bis = clamp_to_employment(emp, von, bis)
    if von > bis:
        return 0.0
    lt_id = int(leave_type.get("ID") or 0)
    factor = charge_factor(leave_type, emp.hrs_day)
    hours = 0.0
    for _d, rec in _dated_entries(absences, von, bis):
        if int(rec.get("LEAVETYPID") or 0) == lt_id:
            hours += absence_hours(emp, rec, leave_type, holidays) * factor
    if in_days:
        return hours / emp.hrs_day if emp.hrs_day > EPSILON else 0.0
    return hours


@dataclass(frozen=True)
class LeaveAccount:
    """Spec 3.7.1 Nr. 5: die fünf Werte einer Anspruchs-Statistikzeile."""

    entitlement: float  # ENTITLEMNT ("Normal")
    rest: float  # REST (carry-over from previous year)
    total: float  # entitlement + rest
    taken: float
    remaining: float  # total − taken (negative = exceeded, 3.7.1 no. 6)


def _entitlements_by_year_type(
    entitlements: Iterable[Record],
) -> dict[tuple[int, int], Record]:
    return {
        (int(rec.get("YEAR") or 0), int(rec.get("LEAVETYPID") or 0)): rec
        for rec in entitlements
    }


def leave_account(
    emp: EmployeeContext,
    year: int,
    leave_type: Record,
    *,
    holidays: Holidays,
    entitlements: Iterable[Record],
    absences: Iterable[Record],
) -> LeaveAccount:
    """Spec 3.7.1: Anspruchskonto für (Mitarbeiter × Jahr × Abwesenheitsart)."""
    rec = _entitlements_by_year_type(entitlements).get(
        (year, int(leave_type.get("ID") or 0))
    )
    entitlement = float(rec.get("ENTITLEMNT") or 0.0) if rec else 0.0
    rest = float(rec.get("REST") or 0.0) if rec else 0.0
    in_days = bool(rec.get("INDAYS", 1)) if rec else True
    taken = leave_taken(
        emp,
        leave_type,
        date(year, 1, 1),
        date(year, 12, 31),
        holidays=holidays,
        absences=absences,
        in_days=in_days,
    )
    total = entitlement + rest
    return LeaveAccount(entitlement, rest, total, taken, total - taken)


def annual_close(
    emp: EmployeeContext,
    year: int,
    *,
    holidays: Holidays,
    leave_types: Iterable[Record],
    entitlements: Iterable[Record],
    absences: Iterable[Record],
    keep_entitlements: bool = False,
) -> list[Record]:
    """Spec 3.7.2 Nr. 7: Jahresabschluss — die 5LEAEN-Zeilen für Jahr+1 berechnen.

    Reine Berechnung: liefert die neuen/aktualisierten Zeilen als dicts (YEAR,
    LEAVETYPID, ENTITLEMNT, REST, INDAYS); das Persistieren ist Sache des
    Aufrufers. ``keep_entitlements`` ist die Dialog-Option „Urlaubsansprüche
    bleiben im Folgejahr gleich".
    """
    by_year_type = _entitlements_by_year_type(entitlements)
    result = []
    for lt in leave_types:
        if not lt.get("ENTITLED"):
            continue
        carry = bool(lt.get("CARRYFWD"))
        if not (carry or keep_entitlements):
            continue
        lt_id = int(lt.get("ID") or 0)
        cur = by_year_type.get((year, lt_id))
        normal = float(cur.get("ENTITLEMNT") or 0.0) if cur else 0.0
        rest = float(cur.get("REST") or 0.0) if cur else 0.0
        in_days = bool(cur.get("INDAYS", 1)) if cur else True
        taken = leave_taken(
            emp,
            lt,
            date(year, 1, 1),
            date(year, 12, 31),
            holidays=holidays,
            absences=absences,
            in_days=in_days,
        )
        rest_new = (normal + rest - taken) if carry else 0.0
        if keep_entitlements:
            normal_new = normal
        else:
            nxt = by_year_type.get((year + 1, lt_id))
            normal_new = (
                float(nxt.get("ENTITLEMNT") or 0.0)
                if nxt
                else float(lt.get("STDENTIT") or 0.0)
            )
        result.append(
            {
                "YEAR": year + 1,
                "LEAVETYPID": lt_id,
                "ENTITLEMNT": normal_new,
                "REST": rest_new,
                "INDAYS": int(in_days),
            }
        )
    return result


def forfeit_rest(
    emp: EmployeeContext,
    cutoff: date,
    *,
    holidays: Holidays,
    leave_types: Iterable[Record],
    entitlements: Iterable[Record],
    absences: Iterable[Record],
) -> list[Record]:
    """Spec 3.7.3 Nr. 8: REST auf den Verbrauch bis zum Stichtag kappen.

    Liefert die zu aktualisierenden Zeilen (YEAR, LEAVETYPID, REST); REST wird
    nur je verringert, nie erhöht, ENTITLEMNT bleibt unangetastet.
    """
    year = cutoff.year
    by_year_type = _entitlements_by_year_type(entitlements)
    result = []
    for lt in leave_types:
        if not lt.get("ENTITLED"):
            continue
        lt_id = int(lt.get("ID") or 0)
        rec = by_year_type.get((year, lt_id))
        rest = float(rec.get("REST") or 0.0) if rec else 0.0
        in_days = bool(rec.get("INDAYS", 1)) if rec else True
        taken = leave_taken(
            emp,
            lt,
            date(year, 1, 1),
            cutoff,
            holidays=holidays,
            absences=absences,
            in_days=in_days,
        )
        if rest - taken > EPSILON:
            result.append({"YEAR": year, "LEAVETYPID": lt_id, "REST": taken})
    return result


# ── 3.8 Extra charges (5XCHAR) ──────────────────────────────────


def _window_day_parts(
    start: int, end: int
) -> list[tuple[int, tuple[int, int]]]:
    """Split a (start, end) minute window into per-day parts (3.4.3 no. 8).

    End 0 counts as 1440; end <= start crosses midnight and yields a part on
    the entry day (offset 0) and one on the next day (offset 1).
    """
    end = DAY_MINUTES if end == 0 else end
    if end <= start:
        parts = [(0, (start, DAY_MINUTES)), (1, (0, end))]
    else:
        parts = [(0, (start, end))]
    return [(off, (s, e)) for off, (s, e) in parts if e > s]


def _charge_window_parts(start: int, end: int) -> list[tuple[int, int]]:
    """Spec 3.8.3 no. 9: charge window; day-change windows checked two-part."""
    return [part for _off, part in _window_day_parts(start, end)]


def daily_work_intervals(
    emp: EmployeeContext,
    von: date,
    bis: date,
    *,
    holidays: Holidays,
    shifts_by_id: Mapping[int, Record],
    manual_shifts: Iterable[Record] = (),
    cycle_shifts: Iterable[Record] = (),
    special_shifts: Iterable[Record] = (),
) -> dict[date, list[tuple[int, int]]]:
    """Arbeitsseitige Intervalle je Kalendertag für den Anrechnungs-Schnitt.

    Nutzt die Schicht-Zeitfenster STARTEND{idx} (idx = Feiertag ? 7 :
    Wochentag, bis zu 3 Teilfenster, Spec 3.8.3 Nr. 10), teilt tags-
    übergreifende Fenster auf die Kalendertage d und d+1 (3.4.3 Nr. 8),
    beachtet NOEXTRA (3.8.3 Nr. 13; bei 5SPSHI über die referenzierte Schicht,
    wenn SHIFTID gesetzt, sonst das eigene Feld) und die Ersetzungsregel für
    Arbeitszeitabweichungen (3.4.4 Nr. 12).
    """
    von, bis = clamp_to_employment(emp, von, bis)
    intervals: dict[date, list[tuple[int, int]]] = {}
    if von > bis:
        return intervals
    scan_from = von - timedelta(days=1)  # Überlauf vom Vortag
    special = list(_dated_entries(special_shifts, scan_from, bis))
    replaced = _replaced_days(special)

    def add(day: date, windows: list[tuple[int, int]]) -> None:
        for start, end in windows:
            if (start, end) == (0, 0):
                continue  # zero-minutes window = not defined
            for off, part in _window_day_parts(start, end):
                target = day + timedelta(days=off)
                if von <= target <= bis:
                    intervals.setdefault(target, []).append(part)

    for source in (manual_shifts, cycle_shifts):
        for d, rec in _dated_entries(source, scan_from, bis):
            if d in replaced:
                continue
            shift = shifts_by_id.get(int(rec.get("SHIFTID") or 0))
            if shift is None or shift.get("NOEXTRA"):
                continue
            idx = day_index(d, holidays)
            add(d, parse_startend(str(shift.get(f"STARTEND{idx}") or "")))
    for d, rec in special:
        shift_id = int(rec.get("SHIFTID") or 0)
        if shift_id:
            ref = shifts_by_id.get(shift_id)
            noextra = bool(ref.get("NOEXTRA")) if ref else False
        else:
            noextra = bool(rec.get("NOEXTRA"))
        if noextra:
            continue
        add(d, parse_startend(str(rec.get("STARTEND") or "")))
    return intervals


def extracharge_hours_on_day(
    extracharge: Record,
    d: date,
    work_intervals: Iterable[tuple[int, int]],
    *,
    holidays: Holidays,
    half_as_full: bool = False,
) -> float:
    """Spec 3.8.2/3.8.3: zuschlagsfähige Stunden einer Zuschlagsart an einem Tag.

    ``work_intervals`` sind die Arbeitsintervalle des Tages (Minuten) aus
    :func:`daily_work_intervals`. ``half_as_full`` ist die Berichtsoption
    „halbe wie ganze Feiertage behandeln" (3.8.2 Nr. 8).
    """
    if int(extracharge.get("VALIDITY") or 0) == 1:  # festes Datum (3.8.2 Nr. 5)
        if to_date(extracharge.get("DATE")) != d:
            return 0.0
    else:  # 7-slot weekday mask Mon..Sun (3.8.2 no. 6, D-36)
        if not parse_day_mask(str(extracharge.get("VALIDDAYS") or ""), 7)[d.weekday()]:
            return 0.0
    holrule = int(extracharge.get("HOLRULE") or 0)
    interval = holidays.get(d)
    half: tuple[int, int] | None = None
    if holrule == 1:  # only on holidays
        if interval is None:
            return 0.0
        if interval != 0 and not half_as_full:
            half = (
                (HALF_DAY_MINUTES, DAY_MINUTES) if interval == 1 else (0, HALF_DAY_MINUTES)
            )
    elif holrule == 2 and interval is not None:  # not on holidays
        if interval == 0 or half_as_full:
            return 0.0
        half = (
            (HALF_DAY_MINUTES, DAY_MINUTES) if interval == 1 else (0, HALF_DAY_MINUTES)
        )
    parts = _charge_window_parts(
        int(extracharge.get("START") or 0), int(extracharge.get("END") or 0)
    )
    if half is not None:  # restrict to one day half (3.8.2 no. 8)
        parts = [
            (max(s, half[0]), min(e, half[1]))
            for s, e in parts
            if min(e, half[1]) > max(s, half[0])
        ]
    minutes = 0
    for ps, pe in parts:
        for ws, we in work_intervals:
            minutes += max(0, min(pe, we) - max(ps, ws))
    return minutes / 60.0


def get_extracharge_hours(
    emp: EmployeeContext,
    extracharge: Record,
    von: date,
    bis: date,
    *,
    holidays: Holidays,
    shifts_by_id: Mapping[int, Record],
    manual_shifts: Iterable[Record] = (),
    cycle_shifts: Iterable[Record] = (),
    special_shifts: Iterable[Record] = (),
    half_as_full: bool = False,
) -> float:
    """Zuschlagsfähige Stunden einer Zuschlagsart über [von, bis] (3.8)."""
    von, bis = clamp_to_employment(emp, von, bis)
    if von > bis:
        return 0.0
    intervals = daily_work_intervals(
        emp,
        von,
        bis,
        holidays=holidays,
        shifts_by_id=shifts_by_id,
        manual_shifts=manual_shifts,
        cycle_shifts=cycle_shifts,
        special_shifts=special_shifts,
    )
    total = 0.0
    for d, work in intervals.items():
        total += extracharge_hours_on_day(
            extracharge, d, work, holidays=holidays, half_as_full=half_as_full
        )
    return total


# ── 3.9 Personaltabelle und Auslastung ──────────────────────────


def shift_assignment_counts(
    emp: EmployeeContext,
    von: date,
    bis: date,
    *,
    manual_shifts: Iterable[Record] = (),
    cycle_shifts: Iterable[Record] = (),
) -> dict[int, int]:
    """Spec 3.9.3 Nr. 4: je Schichtart die Zahl der Zuweisungen im Zeitraum."""
    von, bis = clamp_to_employment(emp, von, bis)
    counts: dict[int, int] = {}
    if von > bis:
        return counts
    for source in (manual_shifts, cycle_shifts):
        for _d, rec in _dated_entries(source, von, bis):
            shift_id = int(rec.get("SHIFTID") or 0)
            if shift_id:
                counts[shift_id] = counts.get(shift_id, 0) + 1
    return counts


def count_special_shifts(
    emp: EmployeeContext,
    von: date,
    bis: date,
    special_shifts: Iterable[Record] = (),
) -> int:
    """Spec 3.9.2 Nr. 2: Zahl der Sonderdienste im Zeitraum."""
    von, bis = clamp_to_employment(emp, von, bis)
    if von > bis:
        return 0
    return sum(1 for _ in _dated_entries(special_shifts, von, bis))


def personnel_table_row(
    emp: EmployeeContext,
    von: date,
    bis: date,
    *,
    holidays: Holidays,
    shifts_by_id: Mapping[int, Record],
    manual_shifts: Iterable[Record] = (),
    cycle_shifts: Iterable[Record] = (),
    special_shifts: Iterable[Record] = (),
    absences: Iterable[Record] = (),
    leave_types_by_id: Mapping[int, Record] | None = None,
    bookings: Iterable[Record] = (),
) -> dict[str, float]:
    """Spec 3.9.2 Nr. 2: die Standard-Personaltabellen-Spalten eines Mitarbeiters."""
    leave_types_by_id = leave_types_by_id or {}
    plan = dict(
        holidays=holidays,
        shifts_by_id=shifts_by_id,
        manual_shifts=manual_shifts,
        cycle_shifts=cycle_shifts,
        special_shifts=special_shifts,
    )
    ist = get_actual_hours(
        emp,
        von,
        bis,
        absences=absences,
        leave_types_by_id=leave_types_by_id,
        bookings=bookings,
        **plan,
    )
    soll = get_nominal_hours(emp, von, bis, holidays=holidays, bookings=bookings)
    counts = duty_day_counts(
        emp,
        von,
        bis,
        holidays=holidays,
        manual_shifts=manual_shifts,
        cycle_shifts=cycle_shifts,
        special_shifts=special_shifts,
    )
    paid = absence_sums(
        emp,
        von,
        bis,
        holidays=holidays,
        absences=absences,
        leave_types_by_id=leave_types_by_id,
    ).charged
    return {
        "iststunden": ist,
        "sollstunden": soll,
        "saldo": ist - soll,
        "arbeitszeit": get_work_hours(emp, von, bis, **plan),
        "abwesenheit_bezahlt": paid,
        "sonntag": counts[6],
        "feiertag": counts[HOLIDAY_INDEX],
        "sonderdienste": count_special_shifts(emp, von, bis, special_shifts),
    }


def count_assigned(
    entries_by_employee: Mapping[Any, Iterable[Record]],
    d: date,
    shift_ids: Iterable[int],
) -> int:
    """Spec 3.9.4 Nr. 8: die der/den Schichtart(en) an Tag d zugewiesenen MA.

    ``entries_by_employee`` bildet einen MA-Schlüssel auf seine Planeinträge ab
    (5MASHI + expandierte 5CYASS + 5SPSHI mit SHIFTID); jeder MA zählt höchstens einmal.
    """
    wanted = set(shift_ids)
    count = 0
    for entries in entries_by_employee.values():
        for rec in entries:
            if (
                to_date(rec.get("DATE")) == d
                and int(rec.get("SHIFTID") or 0) in wanted
            ):
                count += 1
                break
    return count


def utilization_status(actual: int, min_demand: int, max_demand: int) -> int:
    """Spec 3.9.4 no. 9: −1 = understaffed, +1 = overstaffed, 0 = within demand."""
    if actual < min_demand:
        return -1
    if actual > max_demand:
        return 1
    return 0


def demand_for_day(
    demands: Iterable[Record],
    d: date,
    *,
    holidays: Holidays,
    shift_id: int | None = None,
) -> tuple[int, int] | None:
    """Schlägt den (MIN, MAX)-Bedarf aus 5SHDEM-artigen Sätzen für Tag d nach.

    Die Sätze tragen WEEKDAY als Tag-Index 0..7 (7 = Feiertags-Slot); der
    Aufrufer filtert nach Gruppe vor. Liefert None, wenn kein Bedarf definiert ist.
    """
    idx = day_index(d, holidays)
    for rec in demands:
        weekday = rec.get("WEEKDAY")
        if weekday is None or int(weekday) != idx:
            continue
        if shift_id is not None and int(rec.get("SHIFTID") or 0) != shift_id:
            continue
        return int(rec.get("MIN") or 0), int(rec.get("MAX") or 0)
    return None


# ── 5CYASS cycle expansion (spec 6.3 / 5.7) ─────────────────────


def expand_cycle_assignments(
    assignments: Iterable[Record],
    *,
    cycles: Iterable[Record],
    cycle_entries: Iterable[Record],
    cycle_exceptions: Iterable[Record] = (),
    von: date,
    bis: date,
) -> list[Record]:
    """Expand 5CYASS assignments into concrete day duties within [von, bis].

    Per assignment day the cycle position is taken modulo the cycle length
    (rolling application, R6.3-7). ENTRANCE is the 0-based entry offset: for
    week models (5CYCLE.UNIT = 1) the model row (week) valid at the assignment
    start — within a row the weekday of the date aligns (model rows run
    Mon..Sun, R6.3-4); for day models the day position at the start (R6.3-5).
    Cycle positions without a 5CYENT entry (or SHIFTID 0) are free days
    (D-50); 5CYEXC records suppress the cycle duty on their DATE.

    Returns 5MASHI-shaped dicts (EMPLOYEEID, DATE, SHIFTID, WORKPLACID).
    """
    cycles_by_id = {int(rec.get("ID") or 0): rec for rec in cycles}
    entries_by_cycle: dict[int, dict[int, Record]] = {}
    for rec in cycle_entries:
        positions = entries_by_cycle.setdefault(int(rec.get("CYCLEEID") or 0), {})
        positions[int(rec.get("INDEX") or 0)] = rec
    exceptions: dict[tuple[int, int], set[date]] = {}
    for rec in cycle_exceptions:
        d = to_date(rec.get("DATE"))
        if d is not None:
            key = (int(rec.get("EMPLOYEEID") or 0), int(rec.get("CYCLEASSID") or 0))
            exceptions.setdefault(key, set()).add(d)

    result: list[Record] = []
    for ass in assignments:
        cycle = cycles_by_id.get(int(ass.get("CYCLEID") or 0))
        start = to_date(ass.get("START"))
        if cycle is None or start is None:
            continue
        size = int(cycle.get("SIZE") or 0)
        week_model = int(cycle.get("UNIT") or 0) == 1
        length = size * 7 if week_model else size
        if length <= 0:
            continue
        end = to_date(ass.get("END"))
        lo = max(von, start)
        hi = bis if end is None else min(bis, end)
        entrance = int(ass.get("ENTRANCE") or 0)
        base = entrance * 7 + start.weekday() if week_model else entrance
        positions = entries_by_cycle.get(int(cycle.get("ID") or 0), {})
        employee_id = int(ass.get("EMPLOYEEID") or 0)
        skip = exceptions.get((employee_id, int(ass.get("ID") or 0)), set())
        d = lo
        while d <= hi:
            entry = positions.get((base + (d - start).days) % length)
            if entry is not None and int(entry.get("SHIFTID") or 0) and d not in skip:
                result.append(
                    {
                        "EMPLOYEEID": employee_id,
                        "DATE": d.isoformat(),
                        "SHIFTID": int(entry.get("SHIFTID") or 0),
                        "WORKPLACID": int(entry.get("WORKPLACID") or 0),
                    }
                )
            d += timedelta(days=1)
    return result
