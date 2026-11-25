"""Check the CSV files that ok_votes_extractor.py writes.

Usage:
    python audit_output.py VotesDetail.csv VoteRollCall.csv

It prints how well the member-level rows match the vote counts, how many fields are
blank, and a few labels that look wrong. It only reads the files; it changes nothing.
Requires pandas (pip install pandas).
"""
import sys

import pandas as pd

COUNTS = ["Yea", "Nay", "CP", "Excused", "Vacant"]
STANDARD_TYPES = {"Third Reading", "Fourth Reading", "General Order"}
NOT_NAMES = ["VAC", "AM", "PM"]


def pct(part, whole):
    return "%.1f%%" % (100.0 * part / whole) if whole else "n/a"


def main(detail_path, rollcall_path):
    d = pd.read_csv(detail_path, dtype=str, keep_default_na=False)
    r = pd.read_csv(rollcall_path, dtype=str, keep_default_na=False)
    for c in COUNTS:
        d[c] = pd.to_numeric(d[c])
    d["seats"] = d[COUNTS].sum(axis=1)
    d["date"] = pd.to_datetime(d["Date"], format="%m/%d/%Y")
    d["year"] = d["date"].dt.year
    n = len(d)

    print("== Votes")
    print("votes:", n, "| dates:", d["date"].min().date(), "to", d["date"].max().date())

    print("\n== Do the counts add up to the size of the chamber?")
    in_range = d["seats"].between(99, 105)
    print("votes with 99 to 105 seats:", int(in_range.sum()), "of", n, "(%.2f%%)" % (100.0 * in_range.sum() / n))
    print("votes with exactly 101 seats:", int((d["seats"] == 101).sum()))
    odd = d.loc[~in_range, ["Unique Index", "Bill Number", "Date", "seats"]]
    print("votes outside 99 to 105:")
    print(odd.to_string(index=False))

    print("\n== Does every counted seat have a named member?")
    rows = r.groupby("Votes Detail Index").size()
    d["names"] = d["Unique Index"].map(rows).fillna(0).astype(int)
    seats, names = int(d["seats"].sum()), int(d["names"].sum())
    print("counted seats:", seats, "| member rows:", names, "| share:", pct(names, seats))
    vacant = int(d["Vacant"].sum())
    print("counted seats without vacancies:", seats - vacant, "| share:", pct(names, seats - vacant))
    print("votes where names equal seats:", int((d["names"] == d["seats"]).sum()), "of", n)
    print("votes with no member rows at all:", int((d["names"] == 0).sum()))
    by_vote = r.groupby(["Votes Detail Index", "Vote"]).size().unstack(fill_value=0)
    for label in ["Yea", "Nay", "Excused", "CP"]:
        got = int(by_vote[label].sum()) if label in by_vote else 0
        print("  %-8s member rows %9d of %9d counted (%s)" % (label, got, int(d[label].sum()), pct(got, int(d[label].sum()))))
    per_year = d.groupby("year")[["names", "seats"]].sum()
    print("share of seats with a name, by year:")
    for year, row in per_year.iterrows():
        print("  %d  %s" % (year, pct(row["names"], row["seats"])))

    print("\n== Blank or odd fields")
    print("blank Bill Number:", int((d["Bill Number"] == "").sum()), "(%s)" % pct((d["Bill Number"] == "").sum(), n))
    print("blank Result:", int((d["Result"] == "").sum()), "(%s)" % pct((d["Result"] == "").sum(), n))
    print("Result values:", d["Result"].value_counts().to_dict())
    other_type = ~d["Type"].isin(STANDARD_TYPES)
    print("Type is not Third Reading, Fourth Reading or General Order:", int(other_type.sum()),
          "votes,", d.loc[other_type, "Type"].nunique(), "different values")

    print("\n== Location against the bill prefix")
    prefix = d["Bill Number"].str.extract(r"^\s*([A-Za-z]+)")[0].fillna("(blank)")
    keep = prefix.where(prefix.isin(["HB", "SB", "HJR", "SJR", "HCR", "SCR", "(blank)"]), "other")
    place = d["Location"].where(d["Location"].isin(["House Floor", "Senate Floor"]), "other")
    print(pd.crosstab(keep.rename("bill prefix"), place.rename("Location")).to_string())
    print("seats per vote when Location is Senate Floor: min %d, max %d"
          % (d.loc[d["Location"] == "Senate Floor", "seats"].min(), d.loc[d["Location"] == "Senate Floor", "seats"].max()))

    print("\n== Member column")
    print("distinct members:", r["Member"].nunique())
    counts = r["Member"].value_counts()
    for name in NOT_NAMES:
        print("  rows where Member is %s: %d" % (name, int(counts.get(name, 0))))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
