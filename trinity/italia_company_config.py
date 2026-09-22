"""Small, dated source configuration for the Italia generalization gate."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DocumentSource:
    title: str
    url: str
    published_at: str
    document_type: str
    index_url: str


@dataclass(frozen=True)
class CompanyConfig:
    company_name: str
    ticker: str
    isin: str | None
    ir_base_url: str
    schema_type: str
    documents: tuple[DocumentSource, ...]
    extract_report_guidance: bool = False


COMPANIES = {
    "TPRO.MI": CompanyConfig("Technoprobe S.p.A.", "TPRO.MI", "IT0005482333",
        "https://www.technoprobe.com/investors/investor-relations", "INDUSTRIAL", ()),
    "PRY.MI": CompanyConfig("Prysmian S.p.A.", "PRY.MI", None,
        "https://www.prysmian.com/en/investors", "INDUSTRIAL", (
        DocumentSource("Prysmian 1H26 results presentation",
            "https://www.prysmian.com/sites/www.prysmian.com/files/media/documents/investors/Prysmian_2Q26_Presentation_def.pdf",
            "2026-07-30", "RESULTS", "https://www.prysmian.com/en/investors/results-centre"),
        DocumentSource("Prysmian half-year financial report",
            "https://www.prysmian.com/sites/www.prysmian.com/files/media/documents/investors/ING_Relazione_Finanziaria_30_giugno_2026.pdf",
            "2026-07-31", "FINANCIAL_REPORT", "https://www.prysmian.com/en/investors/results-centre"),
    ), True),
    "ISP.MI": CompanyConfig("Intesa Sanpaolo S.p.A.", "ISP.MI", None,
        "https://group.intesasanpaolo.com/en/investor-relations", "BANK", (
        DocumentSource("Intesa Sanpaolo 1H26 results",
            "https://group.intesasanpaolo.com/content/dam/portalgroup/repository-documenti/investor-relations/comunicati-stampa-en/2026/07/20260729_Ris1H26_uk.pdf",
            "2026-07-29", "RESULTS", "https://group.intesasanpaolo.com/en/investor-relations/results"),
        DocumentSource("Intesa Sanpaolo half-year report",
            "https://group.intesasanpaolo.com/content/dam/portalgroup/repository-documenti/investor-relations/bilanci-relazioni-en/2026/30062026_Half-yearly_report.pdf",
            "2026-08-11", "FINANCIAL_REPORT", "https://group.intesasanpaolo.com/en/investor-relations/results"),
    )),
    "ENEL.MI": CompanyConfig("Enel S.p.A.", "ENEL.MI", None,
        "https://www.enel.com/investors", "UTILITY", (
        DocumentSource("Enel first-half 2026 results",
            "https://www.enel.com/content/dam/enel-common/press/en/2026-july/PR-Enel-results-1H-2026.pdf",
            "2026-07-30", "RESULTS",
            "https://www.enel.com/media/explore/search-press-releases/press/2026/07/enel-the-portfolio-of-international-activities-drives-growth-in-the-first-half-of-2026-more-than-offsetting-lower-margins-in-italy-eps-for-the-year-expected-at-the-upper-end-of-the-guidance-range"),
        DocumentSource("Enel half-year financial report",
            "https://www.enel.com/content/dam/enel-com/documenti/investitori/informazioni-finanziarie/2026/interim/en/half-year-financial-report_30June2026.pdf",
            "2026-08-06", "FINANCIAL_REPORT", "https://www.enel.com/investors/financials"),
    )),
    "DLG.MI": CompanyConfig("De' Longhi S.p.A.", "DLG.MI", None,
        "https://www.delonghigroup.com/en/archive", "CONSUMER", (
        DocumentSource("De' Longhi Q2 and H1 2026 results",
            "https://www.delonghigroup.com/sites/default/files/DeLonghi%20-%20press%20release%20Q2-26%20results_E.pdf",
            "2026-07-30", "RESULTS", "https://www.delonghigroup.com/en/archive/results"),
        DocumentSource("De' Longhi interim financial report at June 30 2026",
            "https://www.delonghigroup.com/sites/default/files/Interim%20financial%20report%20at%2030%20June%202026_PER%20PUBBLICAZIONE.pdf",
            "2026-08-28", "FINANCIAL_REPORT", "https://www.delonghigroup.com/en/archive/results"),
    )),
}
