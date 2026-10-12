import sys
from aegis_bioquant.ingestion.outcome_finder import classify_filing_text as f

CASES = [
    ("If approved by the FDA, the drug would launch in 2027.", None),
    ("The FDA has approved LUMVOA for thyroid eye disease.", "APPROVED"),
    ("Acme Announces U.S. FDA Approval of Zetamab. BOSTON", "APPROVED"),
    ("The Company received a Complete Response Letter from the FDA regarding its NDA.", "CRL"),
    ("The FDA extended the PDUFA target action date to March 3, 2027.", "DELAYED"),
    ("Capricor Therapeutics Announces Extension of PDUFA Target Action Date as FDA Continues Review.", "DELAYED"),
    ("We expect that the FDA approves the application in the fourth quarter.", None),
    ("Following receipt of a complete response letter in 2024, the Company resubmitted. The FDA approved the resubmission today.", "APPROVED"),
    ("The drug was approved by the U.S. Food and Drug Administration on June 26.", "APPROVED"),
    ("Pending approval, we will hire a sales force. Upon FDA approval we would launch.", None),
    ("The Company announced that it received a complete response letter. The FDA has approved a different product, Xyz, in 2019.", "CRL"),
    ("Our pipeline update contains no regulatory decisions.", None),
    ("The FDA granted accelerated approval to Tudriqev in combination with nivolumab.", "APPROVED"),
    ("Should the FDA approve the NDA, the Company would receive a milestone.", None),
    ("The Company previously received a complete response letter for this product.", None),
    ("There are currently no U.S. Food and Drug Administration (FDA) approved therapies for KCNT1+ epilepsy.", None),
    ("It is the only FDA approved treatment for this condition.", None),
    ("The FDA has not approved the application.", None),
    ("On July 23, 2026, the U.S. Food and Drug Administration approved Furoscix ReadyFlow for edema.", "APPROVED"),
    ("The FDA approved Joenja for pediatric patients.", "APPROVED"),
]

if __name__ == "__main__":
    ok = True
    for text, want in CASES:
        got = f(text)
        good = (got[0] if got else None) == want
        ok &= good
        print("ok  " if good else "FAIL", str(want).ljust(8), "<-", (got[1][:95] if got else None))
    print("ALL CASES OK" if ok else "SOME CASES FAILED")
    sys.exit(0 if ok else 1)
