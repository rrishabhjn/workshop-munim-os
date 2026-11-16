"""Idempotent Master Data & Active Execution Orders Seed from 'October main'.

Loads Option #1 into Supabase via the backend's `get_supabase_admin()` client
(without touching Zoho Books):
  1. Vendors (47 Suppliers + 5 Piece-Rate Sub-Contractors)
  2. People Roster (6 Office/Supervisor Operators + 18 Salaried Staff/Karigars
     with Proxy Bank Payout Aliases + 4 Contractor Mistris linked to Vendors)
  3. Custodian links on the 4 Supervisor Float `payment_accounts`
  4. Canonical Customer Directory (34 customers with area/landmarks & aliases)
  5. Active Execution Orders + Completed Masosa (`PRJ-OCT-001` .. `PRJ-OCT-015`)
     and their 43 flat furniture items (`project_items`)

Usage:
    .\\backend\\.venv\\Scripts\\python.exe scripts\\seed_october_master.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.db import get_supabase_admin  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Vendors (47 Material/Logistics Suppliers + 5 Piece-Rate Contractors)
# ---------------------------------------------------------------------------
VENDORS: list[dict[str, Any]] = [
    # Piece-Rate Contractors
    {
        "canonical_name": "Manish Cushioning",
        "vendor_kind": "contractor",
        "specialty": "Sofa & Bed Upholstery / Cushioning",
        "aliases": ["Manish Katiriya", "Manish Sofa", "Manish Ji Cushion"],
        "notes": "Piece-rate sofa & bed cushioning contractor across Ambure, Samiksha, Lodha, Aakash.",
    },
    {
        "canonical_name": "Sunil False Ceiling",
        "vendor_kind": "contractor",
        "specialty": "False Ceiling & Paint",
        "aliases": ["Sunil Ceiling", "Sunil Paint", "Sunil"],
        "notes": "Piece-rate false ceiling & paint contractor (e.g. Samiksha site).",
    },
    {
        "canonical_name": "Ritik Fabrication",
        "vendor_kind": "contractor",
        "specialty": "Aluminium Cabin & Profile Fabrication",
        "aliases": ["Ritik", "Ritik Aluminium"],
        "notes": "Aluminium cabin / profile fabrication contractor.",
    },
    {
        "canonical_name": "Prince Jangid",
        "vendor_kind": "contractor",
        "specialty": "Site Installation / Piece-Work",
        "aliases": ["Prince"],
        "notes": "Site piece-rate contractor (Samiksha site).",
    },
    {
        "canonical_name": "Site Electrician",
        "vendor_kind": "contractor",
        "specialty": "Site Electrical Work",
        "aliases": ["Electrician"],
        "notes": "Generic site electrical piece-work payee from October ledger.",
    },
    # Plywood, Doors & Timber Suppliers
    {
        "canonical_name": "R P Wood",
        "vendor_kind": "supplier",
        "specialty": "Plywood & Blockboard",
        "aliases": ["RP Wood", "RP Wood Ply", "R.P. Wood"],
    },
    {
        "canonical_name": "Singh Plywood",
        "vendor_kind": "supplier",
        "specialty": "Plywood & Boards",
        "aliases": ["Singh Ply"],
        "notes": "Chacha tab notes 60% bill arrangement.",
    },
    {
        "canonical_name": "M.D. Door & Board",
        "vendor_kind": "supplier",
        "specialty": "Flush Doors & Boards",
        "aliases": ["MD Door", "MD Door & Board", "MD Doors"],
    },
    {
        "canonical_name": "Shree Parth Timber Pvt Ltd",
        "vendor_kind": "supplier",
        "specialty": "Flexi Ply, Timber & Liner Laminates",
        "aliases": ["Shree Parth Timber", "Parth Timber"],
    },
    {
        "canonical_name": "PG Plywood",
        "vendor_kind": "supplier",
        "specialty": "Plywood (via Dinesh Gurjar)",
        "aliases": ["PG Ply", "Dinesh Gurjar"],
    },
    {
        "canonical_name": "Swastik Plywood & Interior",
        "vendor_kind": "supplier",
        "specialty": "Plywood & Interior Material",
        "aliases": ["Swastik Plywood", "Swastik Ply"],
    },
    {
        "canonical_name": "SG Plywood",
        "vendor_kind": "supplier",
        "specialty": "Plywood",
        "aliases": ["SG Ply", "S.G. Plywood"],
    },
    {
        "canonical_name": "Mahaveer Timber Store",
        "vendor_kind": "supplier",
        "specialty": "Timber & Wood Sizes",
        "aliases": ["Mahaveer Timber"],
    },
    {
        "canonical_name": "SK Furniture Ajmer",
        "vendor_kind": "supplier",
        "specialty": "Wholesale Furniture & Wood Components",
        "aliases": ["SK Furniture", "S.K. Furniture Ajmer"],
    },
    # Laminates, Louvers, Edgeband & Decorative Panels
    {
        "canonical_name": "Shivam Traders",
        "vendor_kind": "supplier",
        "specialty": "Advance/Clio Laminates & Louvers",
        "aliases": ["Shivam Laminates", "Shivam"],
    },
    {
        "canonical_name": "Shiv Shakti Laminates Pvt Ltd",
        "vendor_kind": "supplier",
        "specialty": "Laminates & Acrylic Sheets",
        "aliases": ["Shiv Shakti Laminates", "Shiv Shakti Enterprises", "Shiv Shakti"],
    },
    {
        "canonical_name": "Star Laminate",
        "vendor_kind": "supplier",
        "specialty": "1mm & Liner Laminates",
        "aliases": ["Star Laminates", "Star Mica"],
    },
    {
        "canonical_name": "Vinayak Laminates",
        "vendor_kind": "supplier",
        "specialty": "Laminates & Decorative Sheets",
        "aliases": ["Vinayak Reolaxe", "Vinayak Mica"],
    },
    {
        "canonical_name": "Amba Laminate Pvt Ltd",
        "vendor_kind": "supplier",
        "specialty": "Laminates",
        "aliases": ["Amba Laminate", "Amba Laminates"],
    },
    {
        "canonical_name": "HLMK Frames Pvt Ltd",
        "vendor_kind": "supplier",
        "specialty": "Louvers, Mouldings & Wall Panels",
        "aliases": ["HLMK Frames", "HLMK"],
    },
    {
        "canonical_name": "Amazing Decor",
        "vendor_kind": "supplier",
        "specialty": "Decorative Panels & Interior Highlighters",
        "aliases": ["Amazing Decors"],
    },
    {
        "canonical_name": "Anushka Sales",
        "vendor_kind": "supplier",
        "specialty": "Hytech PVC Edgeband Tape",
        "aliases": ["Anushka Baheti", "Hytech Edgeband"],
    },
    {
        "canonical_name": "Nawal Kishor Modi",
        "vendor_kind": "supplier",
        "specialty": "Laminates / Local Trade Material",
        "aliases": ["Nawal Modi"],
    },
    {
        "canonical_name": "Rajasthan Plastic",
        "vendor_kind": "supplier",
        "specialty": "PVC Strips & Wood-Matt Profiles",
        "aliases": ["Rajasthan Plastics"],
    },
    # Hardware, Kitchen Fittings & Locks
    {
        "canonical_name": "Abhinandan Associates",
        "vendor_kind": "supplier",
        "specialty": "Hafele, Slido, Matrixbox, Minifix & Fevicol SWR",
        "aliases": ["Abhiandan", "Abhinandan", "Abhinandan Hardware"],
    },
    {
        "canonical_name": "Polo Hardware Collection",
        "vendor_kind": "supplier",
        "specialty": "Slim Drawers, Magic Corner, Wicker Baskets & Legs",
        "aliases": ["Polo Hardware", "Polo"],
    },
    {
        "canonical_name": "S.K. Shah & Co",
        "vendor_kind": "supplier",
        "specialty": "Ebco Hinges, Channels & G-Profiles (Jodhpur)",
        "aliases": ["SK Shah", "SK Shah Ebbco Jodhpur", "SK Shah Ebco"],
    },
    {
        "canonical_name": "Lakshya Sales",
        "vendor_kind": "supplier",
        "specialty": "Digital Fingerprint & Mortise Locks",
        "aliases": ["Lakshya Locks"],
    },
    {
        "canonical_name": "Balaji Hardware & Electricals",
        "vendor_kind": "supplier",
        "specialty": "Local Hardware, Screws & Fasteners",
        "aliases": ["Balaji Hardware"],
    },
    {
        "canonical_name": "Adinath Hardware",
        "vendor_kind": "supplier",
        "specialty": "Hardware & Fittings",
        "aliases": ["Adinath"],
    },
    {
        "canonical_name": "Giriraj Sharma",
        "vendor_kind": "supplier",
        "specialty": "Hinges (Kabje) & Local Fittings",
        "aliases": ["Giriraj Kabje"],
    },
    # Foam, Curtains, Fabrics & Flooring
    {
        "canonical_name": "Veenashree Foam Furnishing Pvt Ltd",
        "vendor_kind": "supplier",
        "specialty": "Sleepwell / High-Density Sofa & Mattress Foam",
        "aliases": ["Veenashree Foam", "Veenashree"],
    },
    {
        "canonical_name": "In Weave Furnishing",
        "vendor_kind": "supplier",
        "specialty": "Curtains, Blinds & Sofa Upholstery Fabrics",
        "aliases": ["In Wear", "In Weave", "Inweave"],
    },
    {
        "canonical_name": "Kavya Furnishing",
        "vendor_kind": "supplier",
        "specialty": "Sofa/Table Legs & Furnishing Accessories",
        "aliases": ["Kavya"],
    },
    {
        "canonical_name": "Ms Florim Global Pvt Ltd",
        "vendor_kind": "supplier",
        "specialty": "Wooden / SPC Flooring & Skirting",
        "aliases": ["Florim Global", "Ms Florim Global"],
    },
    # Electricals, Glass, Tools & Workshop Consumables
    {
        "canonical_name": "Deepak Enterprises",
        "vendor_kind": "supplier",
        "specialty": "Apra LED Strips, Drivers, Profile Lights & Fans",
        "aliases": ["Deepak Electricals"],
    },
    {
        "canonical_name": "Shree Rudraksha Electronic",
        "vendor_kind": "supplier",
        "specialty": "Electronics & Appliances",
        "aliases": ["Rudraksha Electronic", "Shree Rudraksha"],
    },
    {
        "canonical_name": "Shree Shyam Glass",
        "vendor_kind": "supplier",
        "specialty": "Mirrors, Toughened & Fluted Profile Glass",
        "aliases": ["Shyam Glass"],
    },
    {
        "canonical_name": "Universal Sharpening",
        "vendor_kind": "supplier",
        "specialty": "Panel Saw & Cutter Blade Grinding",
        "aliases": ["Universal Blade Sharpening"],
    },
    {
        "canonical_name": "Shree Perfect Power Tool",
        "vendor_kind": "supplier",
        "specialty": "Power Tools, Cutters & Spares",
        "aliases": ["Perfect Power Tool", "Perfect Tools"],
    },
    {
        "canonical_name": "Ms Shri Sagar Trader",
        "vendor_kind": "supplier",
        "specialty": "Workshop Consumables & Trade Supplies",
        "aliases": ["Shri Sagar Trader", "Sagar Traders"],
    },
    {
        "canonical_name": "Shri Nimbark Sales",
        "vendor_kind": "supplier",
        "specialty": "Hardware / Trade Supplies",
        "aliases": ["Nimbark Sales"],
    },
    {
        "canonical_name": "Soni Enterprises",
        "vendor_kind": "supplier",
        "specialty": "Trade Supplies",
        "aliases": ["Soni Ent"],
    },
    {
        "canonical_name": "Jai Shiv Industries",
        "vendor_kind": "supplier",
        "specialty": "Industrial / Workshop Supplies",
        "aliases": ["Jai Shiv"],
    },
    {
        "canonical_name": "Choudhary Traders",
        "vendor_kind": "supplier",
        "specialty": "Local Building / Site Supplies",
        "aliases": ["Choudhary"],
    },
    {
        "canonical_name": "Khandelwal Stationary",
        "vendor_kind": "supplier",
        "specialty": "Office & Shop Stationery",
        "aliases": ["Khandelwal Stationery"],
    },
    # Transport, Tempo & Factory Rent
    {
        "canonical_name": "Shree Balaji Ramapeer Transport Agency",
        "vendor_kind": "supplier",
        "specialty": "Intercity Goods Transport & Freight",
        "aliases": ["Balaji Ramapeer Transport", "Ramapeer Transport"],
    },
    {
        "canonical_name": "Mini Truck Transport Company",
        "vendor_kind": "supplier",
        "specialty": "Mini Truck / Site Delivery Freight",
        "aliases": ["Mini Truck Transport"],
    },
    {
        "canonical_name": "Anil Kumar Chatrani (Jhulelal Transport)",
        "vendor_kind": "supplier",
        "specialty": "Freight & Transport",
        "aliases": ["Jhulelal Transport", "Anil Kumar Chatrani"],
    },
    {
        "canonical_name": "Deepak Bhatt Tempo",
        "vendor_kind": "supplier",
        "specialty": "Local Beawar Tempo Dispatch",
        "aliases": ["Deepak Tempo", "Deepak Bhatt"],
    },
    {
        "canonical_name": "Pintu Auto",
        "vendor_kind": "supplier",
        "specialty": "Local Auto / Small Material Dispatch",
        "aliases": ["Pintu Tempo"],
    },
    {
        "canonical_name": "JP Minerals",
        "vendor_kind": "supplier",
        "specialty": "Workshop / Factory Premises Rent",
        "aliases": ["J.P. Minerals"],
        "notes": "₹61,950 factory/godown rent entry in October ledger.",
    },
]


# ---------------------------------------------------------------------------
# 2. People Roster (Operators + 18 Salaried Staff/Karigars + 4 Contractor Mistris)
# ---------------------------------------------------------------------------
PEOPLE: list[dict[str, Any]] = [
    {
        "full_name": "Ansh",
        "person_type": "operator",
        "role_tags": ["purchase", "inventory", "float_custodian"],
        "aliases": ["Ansh Ji", "Ansh Bhaiya", "Ansh Sir"],
        "payout_aliases": [],
        "can_login_prod": True,
        "can_login_munim": True,
        "is_default_purchase": True,
        "monthly_salary_inr": 0,
    },
    {
        "full_name": "Manu Bhaiya",
        "person_type": "operator",
        "role_tags": ["supervisor", "float_custodian"],
        "aliases": ["Manu", "Manu Ji"],
        "payout_aliases": [],
        "can_login_prod": True,
        "can_login_munim": True,
        "monthly_salary_inr": 0,
    },
    {
        "full_name": "Kamlesh Bhaiya",
        "person_type": "operator",
        "role_tags": ["supervisor", "petty_cash", "float_custodian"],
        "aliases": ["Kamlesh", "Kamlesh Ji", "Kamlesh Singh"],
        "payout_aliases": [],
        "can_login_prod": True,
        "can_login_munim": True,
        "monthly_salary_inr": 0,
    },
    {
        "full_name": "Harshit Bhaiya",
        "person_type": "operator",
        "role_tags": ["coordinator", "shop_ops"],
        "aliases": ["Harshit", "Harshit Ji"],
        "payout_aliases": [],
        "can_login_prod": True,
        "can_login_munim": False,
        "monthly_salary_inr": 4370,
    },
    # 18 Salaried Staff & Karigars (from October `ledger` Cols I:Z)
    {
        "full_name": "Prakash Ji",
        "person_type": "salaried_karigar",
        "role_tags": ["site_supervisor", "senior_karigar", "sizing", "float_custodian"],
        "aliases": ["Prakash Chand", "Prakash"],
        "payout_aliases": [],
        "monthly_salary_inr": 0,
        "metadata": {
            "october_note": "Jaipur/Outstation Site Lead & Float custodian; also receives proxy bank transfers for Narender Ji & Mukesh Khati."
        },
    },
    {
        "full_name": "Arvind Ji",
        "person_type": "salaried_karigar",
        "role_tags": ["designer", "estimator", "sizing"],
        "aliases": ["Arvind Sharma", "Arvind"],
        "payout_aliases": [],
        "monthly_salary_inr": 39000,
        "metadata": {"october_note": "Senior designer/estimator (~₹39,000–₹42,000/mo)."},
    },
    {
        "full_name": "Lala Ji",
        "person_type": "salaried_karigar",
        "role_tags": ["senior_karigar", "shop_mistri"],
        "aliases": ["Lala"],
        "payout_aliases": ["Ruksar Bano", "Roksar Bano", "Sanjay Singh"],
        "monthly_salary_inr": 0,
        "metadata": {"proxy_payee_note": "Bank transfers go to Ruksar Bano / Sanjay Singh."},
    },
    {
        "full_name": "Mukesh Sharma",
        "person_type": "salaried_karigar",
        "role_tags": ["senior_karigar", "shop_mistri"],
        "aliases": ["Mukesh Sharma S/o Sohan Lal"],
        "payout_aliases": ["Parkash Singh"],
        "monthly_salary_inr": 39000,
        "metadata": {"proxy_payee_note": "₹39,000 Oct total; proxy bank transfer alias Parkash Singh."},
    },
    {
        "full_name": "Mukesh Khati",
        "person_type": "salaried_karigar",
        "role_tags": ["karigar"],
        "aliases": ["Mukesh Kharti"],
        "payout_aliases": ["Prakash Chand"],
        "monthly_salary_inr": 0,
        "metadata": {"proxy_payee_note": "Distinct from Mukesh Sharma; ₹1,000 paid via Prakash Chand."},
    },
    {
        "full_name": "Jeetu",
        "person_type": "salaried_karigar",
        "role_tags": ["karigar", "shop_mistri"],
        "aliases": ["Jitender Jangid", "Jeetu Ji"],
        "payout_aliases": [],
        "monthly_salary_inr": 30700,
    },
    {
        "full_name": "Brijesh Ji",
        "person_type": "salaried_karigar",
        "role_tags": ["karigar", "shop_mistri"],
        "aliases": ["Brijesh Sahani", "Mr Brijesh Sahani", "Brijesh"],
        "payout_aliases": [],
        "monthly_salary_inr": 25300,
    },
    {
        "full_name": "Rajan",
        "person_type": "salaried_karigar",
        "role_tags": ["karigar", "shop_mistri"],
        "aliases": ["Rajan Kumar", "Rajan Ji"],
        "payout_aliases": [],
        "monthly_salary_inr": 22500,
    },
    {
        "full_name": "Devilal Ji",
        "person_type": "salaried_karigar",
        "role_tags": ["karigar", "shop_mistri"],
        "aliases": ["Devi Lal Sharma", "Devilal"],
        "payout_aliases": [],
        "monthly_salary_inr": 22000,
    },
    {
        "full_name": "Lalit Ji",
        "person_type": "salaried_karigar",
        "role_tags": ["karigar", "shop_mistri"],
        "aliases": ["Lalit Ji Khati", "Lalit"],
        "payout_aliases": [],
        "monthly_salary_inr": 17790,
    },
    {
        "full_name": "Iliyas",
        "person_type": "salaried_karigar",
        "role_tags": ["karigar"],
        "aliases": ["Iliyas Mo"],
        "payout_aliases": [],
        "monthly_salary_inr": 16718,
    },
    {
        "full_name": "Rajesh Ji",
        "person_type": "salaried_karigar",
        "role_tags": ["karigar"],
        "aliases": ["Rajesh"],
        "payout_aliases": [],
        "monthly_salary_inr": 16225,
    },
    {
        "full_name": "Shahrukh",
        "person_type": "salaried_karigar",
        "role_tags": ["karigar"],
        "aliases": ["Sarukh"],
        "payout_aliases": [],
        "monthly_salary_inr": 14500,
    },
    {
        "full_name": "Narender Ji",
        "person_type": "salaried_karigar",
        "role_tags": ["karigar", "site_crew"],
        "aliases": ["Narender", "Narendra"],
        "payout_aliases": ["Prakash Chand"],
        "monthly_salary_inr": 0,
        "metadata": {"proxy_payee_note": "Jaipur/Ajmer site crew; ₹13,300 Oct FnF paid via Prakash Ji RGB."},
    },
    {
        "full_name": "Bhupender",
        "person_type": "salaried_karigar",
        "role_tags": ["karigar"],
        "aliases": ["Bhupendra"],
        "payout_aliases": [],
        "monthly_salary_inr": 0,
    },
    {
        "full_name": "Bhupender Dad",
        "person_type": "salaried_karigar",
        "role_tags": ["karigar"],
        "aliases": ["Bhupender's Father", "Bhupender Papa"],
        "payout_aliases": [],
        "monthly_salary_inr": 0,
    },
    {
        "full_name": "Kannu",
        "person_type": "salaried_karigar",
        "role_tags": ["helper", "karigar"],
        "aliases": ["Kanu"],
        "payout_aliases": [],
        "monthly_salary_inr": 9100,
    },
    {
        "full_name": "Cook",
        "person_type": "salaried_karigar",
        "role_tags": ["mess_cook"],
        "aliases": ["Mess Cook", "Workshop Cook"],
        "payout_aliases": [],
        "monthly_salary_inr": 9500,
    },
    # 4 Contractor Mistris (linked to `vendors` row so they appear in Mistri dropdowns too)
    {
        "full_name": "Manish Cushioning",
        "person_type": "contractor_mistri",
        "linked_vendor_name": "Manish Cushioning",
        "role_tags": ["upholstery", "sofa_mistri"],
        "aliases": ["Manish Katiriya", "Manish Sofa"],
        "payout_aliases": [],
        "monthly_salary_inr": 0,
    },
    {
        "full_name": "Sunil False Ceiling",
        "person_type": "contractor_mistri",
        "linked_vendor_name": "Sunil False Ceiling",
        "role_tags": ["false_ceiling", "paint"],
        "aliases": ["Sunil Ceiling", "Sunil"],
        "payout_aliases": [],
        "monthly_salary_inr": 0,
    },
    {
        "full_name": "Ritik Fabrication",
        "person_type": "contractor_mistri",
        "linked_vendor_name": "Ritik Fabrication",
        "role_tags": ["aluminium", "fabrication"],
        "aliases": ["Ritik", "Ritik Aluminium"],
        "payout_aliases": [],
        "monthly_salary_inr": 0,
    },
    {
        "full_name": "Prince Jangid",
        "person_type": "contractor_mistri",
        "linked_vendor_name": "Prince Jangid",
        "role_tags": ["site_contractor"],
        "aliases": ["Prince"],
        "payout_aliases": [],
        "monthly_salary_inr": 0,
    },
]

FLOAT_CUSTODIANS: dict[str, str] = {
    "float_prakash": "Prakash Ji",
    "float_kamlesh": "Kamlesh Bhaiya",
    "float_manu": "Manu Bhaiya",
    "float_ansh": "Ansh",
}


# ---------------------------------------------------------------------------
# 3. Canonical Customer Master Directory (34 Customers)
# ---------------------------------------------------------------------------
CUSTOMERS: list[dict[str, Any]] = [
    {"full_name": "Ambure Ji", "area_landmark": "Jaipur (Home & Office)", "aliases": ["Ambure", "Ambure Sir", "Ambure Office"]},
    {"full_name": "Aakash Prajapati", "area_landmark": "Beawar", "aliases": ["Aakash", "Akash Prajapati", "Akash"]},
    {"full_name": "Kaushab", "area_landmark": "Beawar", "aliases": ["Kaushab Ji", "Kaustubh"]},
    {"full_name": "Pragyam Ji", "area_landmark": "Beawar", "aliases": ["Pragyam Bhaiya", "Pragyam"]},
    {"full_name": "Lodha Ji", "area_landmark": "Beawar", "aliases": ["Lodha", "Lodha Ji (A)"]},
    {"full_name": "Samiksha Mam", "area_landmark": "Beawar", "aliases": ["Samiksha", "Samiksha Ji"]},
    {"full_name": "Pandit Ji", "area_landmark": "Beawar", "aliases": ["Pandit"]},
    {
        "full_name": "Singariya Ji",
        "area_landmark": "Beawar",
        "aliases": ["Singharia", "Singariya"],
        "notes": "Confirm with Shreyansh if 'Manak Ji' (₹96,000 cash in ledger R69) is Manak Singariya.",
    },
    {"full_name": "Chacha Ji", "area_landmark": "Beawar", "aliases": ["Chacha"]},
    {"full_name": "Chinmay Agarwal", "area_landmark": "Beawar", "aliases": ["Chinmay", "Chinmay Ji"]},
    {"full_name": "Chair Chittorgarh", "area_landmark": "Chittorgarh", "aliases": ["Chittorgarh Chair", "Chittorgarh Party"]},
    {"full_name": "Mama (Ajmer)", "area_landmark": "Ajmer", "aliases": ["Mama", "Mama Ajmer"]},
    {"full_name": "Kanak", "area_landmark": "Beawar", "aliases": ["Kanak Ji"]},
    {"full_name": "Mahender Ji (Masosa)", "area_landmark": "Masuda / Beawar", "aliases": ["Masosa", "Mahedar Parakh", "Mahender Ji"]},
    # Directory-only customers (from Debtors & Estimate Tabs — no active order seeded yet)
    {"full_name": "Suresh Ji", "area_landmark": "Beawar", "aliases": ["Suresh"], "notes": "Has Kitchen/Crockery estimate tab + ₹15,700 in Debtors."},
    {"full_name": "Mehta Garment", "area_landmark": "Beawar", "aliases": ["Mehta Garments"]},
    {"full_name": "Praveen Mehta", "area_landmark": "Beawar", "aliases": ["Mehta Offset", "Mehta Offset Sofa"]},
    {"full_name": "Mahima", "area_landmark": "Beawar", "aliases": ["Mahima Ji"]},
    {"full_name": "College Estimate", "area_landmark": "Beawar", "aliases": ["College"]},
    {"full_name": "Rishabh Bhandari", "area_landmark": "Beawar", "aliases": ["Rishabh Bhandari Ji"]},
    {"full_name": "Congress Office", "area_landmark": "Beawar", "aliases": ["Congress"]},
    {"full_name": "Jumbo Cafe", "area_landmark": "Beawar", "aliases": ["Jambo Cafe", "Jumbo"]},
    {"full_name": "Anuj Ji", "area_landmark": "Beawar", "aliases": ["Anuj", "Anuj Chacha"]},
    {"full_name": "Rajyawas Customer", "area_landmark": "Rajyawas, Beawar", "aliases": ["Rajyawas"], "notes": "₹50,000 RGB advance in Oct ledger; confirm full customer name."},
    {"full_name": "Sindhi Uncle (Apna Market)", "area_landmark": "Apna Market, Beawar", "aliases": ["Sindhi Uncle"]},
    {"full_name": "Mohan Ji", "area_landmark": "Beawar", "aliases": ["Mohan"]},
    {"full_name": "Mahesh Fancy", "area_landmark": "Beawar", "aliases": ["Mahesh Fancy Store"]},
    {"full_name": "Sunil Ji (Customer)", "area_landmark": "Beawar", "aliases": ["Sunil Ji Debtor"]},
    {"full_name": "Sangeeta Pagaria", "area_landmark": "Beawar", "aliases": ["Sangeeta Ji"]},
    {"full_name": "Hira Nagar Customer", "area_landmark": "Hira Nagar, Beawar", "aliases": ["Hira Nagar"]},
    {"full_name": "Saurabh Bhaiya", "area_landmark": "Beawar", "aliases": ["Saurabh Cloth"]},
    {"full_name": "Ronak", "area_landmark": "Beawar", "aliases": ["Ronak Ji"]},
    {"full_name": "Pikku", "area_landmark": "Beawar", "aliases": ["Pikku Ji"]},
    {"full_name": "Nikki Tilwani", "area_landmark": "Beawar", "aliases": ["Nikki"]},
]


# ---------------------------------------------------------------------------
# 4. Active Execution Orders + Completed Masosa (`PRJ-OCT-001` .. `PRJ-OCT-015`)
# ---------------------------------------------------------------------------
ORDERS: list[dict[str, Any]] = [
    {
        "project_code": "PRJ-OCT-001",
        "customer_name": "Ambure Ji",
        "site_area_landmark": "Jaipur (Home)",
        "rough_scope": "Jaipur Full Home Interior — Kitchen, Wardrobes, Beds, TV Panel, Table & Profile Lights",
        "pipeline_stage": "execution",
        "material_stage": "ordered_pending",
        "payment_stage": "partial_paid",
        "quoted_total": 2300000.00,
        "paid_cache_inr": 1500000.00,
        "metadata": {"source": "October main (Ambure Ji + Debtors + 5 Nov ₹5L cash)"},
        "items": [
            {"item_name": "Kitchen", "stage": "at_site", "assy_mistri": "Prakash Ji"},
            {"item_name": "Wardrobes", "stage": "at_site", "assy_mistri": "Prakash Ji"},
            {"item_name": "Beds", "stage": "at_site", "assy_mistri": "Prakash Ji"},
            {"item_name": "TV Panel", "stage": "at_site", "assy_mistri": "Prakash Ji"},
            {"item_name": "Profile Lights", "stage": "at_site", "assy_mistri": "Prakash Ji"},
            {"item_name": "Table", "stage": "in_shop"},
        ],
    },
    {
        "project_code": "PRJ-OCT-002",
        "customer_name": "Ambure Ji",
        "site_area_landmark": "Jaipur (Ambure Office)",
        "rough_scope": "Ambure Office — Kitchen & Sofa",
        "pipeline_stage": "execution",
        "material_stage": "ordered_pending",
        "payment_stage": "advance_in",
        "quoted_total": 0.00,
        "paid_cache_inr": 0.00,
        "metadata": {"reconciliation_note": "Confirm separate Ambure Office deal value vs Home ₹23,00,000."},
        "items": [
            {"item_name": "Office Kitchen", "stage": "in_shop"},
            {"item_name": "Office Sofa", "stage": "in_shop", "prod_mistri": "Manish Cushioning"},
        ],
    },
    {
        "project_code": "PRJ-OCT-003",
        "customer_name": "Aakash Prajapati",
        "site_area_landmark": "Beawar",
        "rough_scope": "Home Interior, Main Door & Sofa",
        "pipeline_stage": "execution",
        "material_stage": "all_in_shop",
        "payment_stage": "partial_paid",
        "quoted_total": 1021100.00,
        "paid_cache_inr": 900000.00,
        "metadata": {
            "reconciliation_note": "Debtors Left R4 = ₹10,21,100 quoted / ₹9,00,000 received; Right R35 = ₹10,00,000 / ₹6,00,000."
        },
        "items": [
            {"item_name": "Main Door", "stage": "at_site"},
            {"item_name": "Sofa", "stage": "in_shop", "prod_mistri": "Manish Cushioning"},
            {"item_name": "Interior Woodwork", "stage": "at_site"},
        ],
    },
    {
        "project_code": "PRJ-OCT-004",
        "customer_name": "Kaushab",
        "site_area_landmark": "Beawar",
        "rough_scope": "Full Interior (Star & Advance 1mm Laminates + Profile Lights)",
        "pipeline_stage": "execution",
        "material_stage": "ordered_pending",
        "payment_stage": "partial_paid",
        "quoted_total": 700000.00,
        "paid_cache_inr": 400000.00,
        "metadata": {"source": "October main (Kaushab tab + Debtors R18)"},
        "items": [
            {"item_name": "Interior Woodwork", "stage": "in_shop"},
        ],
    },
    {
        "project_code": "PRJ-OCT-005",
        "customer_name": "Pragyam Ji",
        "site_area_landmark": "Beawar",
        "rough_scope": "Kids Room, Beds & Curtains/Tracks",
        "pipeline_stage": "execution",
        "material_stage": "all_in_shop",
        "payment_stage": "partial_paid",
        "quoted_total": 630390.00,
        "paid_cache_inr": 200000.00,
        "metadata": {
            "reconciliation_note": "Debtors shows ₹6,30,390 / ₹2,00,000 paid vs R28 ₹6,50,000 vs R6 ₹55,195 due — confirm with Shreyansh."
        },
        "items": [
            {"item_name": "Kids Room", "stage": "at_site"},
            {"item_name": "Beds", "stage": "at_site"},
            {"item_name": "Curtains & Tracks", "stage": "at_site"},
        ],
    },
    {
        "project_code": "PRJ-OCT-006",
        "customer_name": "Lodha Ji",
        "site_area_landmark": "Beawar",
        "rough_scope": "Kitchen, Wardrobe Temple & Sofa (2 Pcs)",
        "pipeline_stage": "execution",
        "material_stage": "all_in_shop",
        "payment_stage": "partial_paid",
        "quoted_total": 427800.00,
        "paid_cache_inr": 430000.00,
        "metadata": {
            "reconciliation_note": "₹3,50,000 in Debtors + ₹80,000 cash on 14 Nov; also verify 'lodha ji (a)' sub-order."
        },
        "items": [
            {"item_name": "Kitchen", "stage": "at_site"},
            {"item_name": "Wardrobe Temple", "stage": "at_site"},
            {"item_name": "Sofa (2 Pcs)", "stage": "in_shop", "prod_mistri": "Manish Cushioning"},
        ],
    },
    {
        "project_code": "PRJ-OCT-007",
        "customer_name": "Samiksha Mam",
        "site_area_landmark": "Beawar",
        "rough_scope": "Full Interior — False Ceiling, Sofa, TV Panel, Doors, Blinds, Basin Box, Shoe Rack & Fluted Panel",
        "pipeline_stage": "execution",
        "material_stage": "ordered_pending",
        "payment_stage": "partial_paid",
        "quoted_total": 330700.00,
        "paid_cache_inr": 150000.00,
        "metadata": {"source": "October main (SAMIKSHA tab + ₹1L Oct + ₹50K 13 Nov)"},
        "items": [
            {"item_name": "False Ceiling + Paint", "stage": "at_site", "assy_mistri": "Sunil False Ceiling", "specs_notes": "Quoted ₹49,000"},
            {"item_name": "Sofa", "stage": "in_shop", "prod_mistri": "Manish Cushioning", "specs_notes": "Quoted ₹95,000"},
            {"item_name": "TV Panel", "stage": "in_shop", "specs_notes": "Quoted ₹50,000"},
            {"item_name": "Main 2-Door Jali", "stage": "in_shop", "specs_notes": "Quoted ₹30,000"},
            {"item_name": "Bathroom Door", "stage": "in_shop", "specs_notes": "Quoted ₹13,000"},
            {"item_name": "Room Door", "stage": "in_shop", "specs_notes": "Quoted ₹14,000"},
            {"item_name": "3 Blinds", "stage": "in_shop", "specs_notes": "Quoted ₹17,500"},
            {"item_name": "Basin Box & Panelling", "stage": "in_shop", "specs_notes": "Quoted ₹25,000"},
            {"item_name": "Shoe Rack", "stage": "in_shop", "specs_notes": "Quoted ₹25,000"},
            {"item_name": "Outdoor Fluted Panel", "stage": "in_shop", "specs_notes": "Quoted ₹12,000"},
        ],
    },
    {
        "project_code": "PRJ-OCT-008",
        "customer_name": "Pandit Ji",
        "site_area_landmark": "Beawar",
        "rough_scope": "Modular Kitchen (G-Profile, Tandem, BPO, GT-PT), Sliding Almirah & Dressing",
        "pipeline_stage": "execution",
        "material_stage": "all_in_shop",
        "payment_stage": "partial_paid",
        "quoted_total": 266000.00,
        "paid_cache_inr": 160000.00,
        "metadata": {"source": "October main (pandit ji tab + Debtors R9)"},
        "items": [
            {"item_name": "Kitchen", "stage": "in_shop"},
            {"item_name": "Almirah (Sliding)", "stage": "in_shop"},
            {"item_name": "Dressing", "stage": "in_shop"},
        ],
    },
    {
        "project_code": "PRJ-OCT-009",
        "customer_name": "Singariya Ji",
        "site_area_landmark": "Beawar",
        "rough_scope": "Almirah, Bed, Flush Door with Panelling & Dressing",
        "pipeline_stage": "execution",
        "material_stage": "all_in_shop",
        "payment_stage": "partial_paid",
        "quoted_total": 225000.00,
        "paid_cache_inr": 136000.00,
        "metadata": {
            "reconciliation_note": "Debtors shows ₹1,36,000 received vs tab ₹96,000 advance; verify if 'Manak Ji ₹96,000' is same customer."
        },
        "items": [
            {"item_name": "Almirah", "stage": "at_site", "specs_notes": "Quoted ₹96,000"},
            {"item_name": "Bed", "stage": "at_site", "specs_notes": "Quoted ₹60,000"},
            {"item_name": "Flush Door + Panelling", "stage": "at_site", "specs_notes": "Quoted ₹54,000"},
            {"item_name": "Dressing", "stage": "at_site", "specs_notes": "Quoted ₹15,000"},
        ],
    },
    {
        "project_code": "PRJ-OCT-010",
        "customer_name": "Chacha Ji",
        "site_area_landmark": "Beawar",
        "rough_scope": "Kitchen, Almirah, Flush Doors, Louvers & Dressing",
        "pipeline_stage": "execution",
        "material_stage": "ordered_pending",
        "payment_stage": "advance_in",
        "quoted_total": 0.00,
        "paid_cache_inr": 517500.00,
        "metadata": {
            "reconciliation_note": "₹5,00,000 received on main order + Chacha Dressing (₹31,500 quoted / ₹17,500 paid)."
        },
        "items": [
            {"item_name": "Kitchen", "stage": "at_site"},
            {"item_name": "Almirah", "stage": "at_site"},
            {"item_name": "Flush Doors & Louvers", "stage": "at_site"},
            {"item_name": "Dressing", "stage": "in_shop", "specs_notes": "Quoted ₹31,500 (₹17,500 paid)"},
        ],
    },
    {
        "project_code": "PRJ-OCT-011",
        "customer_name": "Chinmay Agarwal",
        "site_area_landmark": "Beawar",
        "rough_scope": "Wardrobe 2-Set, Wooden Flooring, Skirting, Hafele Pulldown & Fingerprint Lock",
        "pipeline_stage": "execution",
        "material_stage": "all_in_shop",
        "payment_stage": "partial_paid",
        "quoted_total": 136400.00,
        "paid_cache_inr": 100000.00,
        "metadata": {"source": "October main (chinmay tab + ₹1L cash 12 Nov)"},
        "items": [
            {"item_name": "Wardrobe 2-Set", "stage": "at_site", "specs_notes": "Quoted ₹86,400"},
            {"item_name": "Flooring & Skirting", "stage": "at_site", "specs_notes": "Quoted ₹34,600"},
            {"item_name": "Hafele Pulldown, Lights & Fingerprint Lock", "stage": "at_site", "specs_notes": "Quoted ₹15,400"},
        ],
    },
    {
        "project_code": "PRJ-OCT-012",
        "customer_name": "Chair Chittorgarh",
        "site_area_landmark": "Chittorgarh",
        "rough_scope": "Outstation Chairs Order",
        "pipeline_stage": "execution",
        "material_stage": "all_in_shop",
        "payment_stage": "partial_paid",
        "quoted_total": 315700.00,
        "paid_cache_inr": 168000.00,
        "metadata": {"reconciliation_note": "Debtors shows ₹1,47,700 balance remaining on ₹3,15,700."},
        "items": [
            {"item_name": "Chairs Batch", "stage": "in_shop"},
        ],
    },
    {
        "project_code": "PRJ-OCT-013",
        "customer_name": "Mama (Ajmer)",
        "site_area_landmark": "Ajmer",
        "rough_scope": "Ajmer Site Woodwork",
        "pipeline_stage": "execution",
        "material_stage": "all_in_shop",
        "payment_stage": "advance_in",
        "quoted_total": 300000.00,
        "paid_cache_inr": 100000.00,
        "metadata": {
            "reconciliation_note": "Debtors R10 = ₹1L paid / ₹2L due; R29 notes 'mama ajmer ₹5,56,000'; ₹1L given directly to Singh Plywood."
        },
        "items": [
            {"item_name": "Ajmer Site Woodwork", "stage": "at_site"},
        ],
    },
    {
        "project_code": "PRJ-OCT-014",
        "customer_name": "Kanak",
        "site_area_landmark": "Beawar",
        "rough_scope": "Site Interior Work",
        "pipeline_stage": "execution",
        "material_stage": "all_in_shop",
        "payment_stage": "advance_in",
        "quoted_total": 0.00,
        "paid_cache_inr": 140000.00,
        "metadata": {"source": "October ledger (₹30K + ₹50K + ₹60K cash received; ₹16,566 Manu Bhaiya site spend)"},
        "items": [
            {"item_name": "Site Interior Work", "stage": "at_site"},
        ],
    },
    {
        "project_code": "PRJ-OCT-015",
        "customer_name": "Mahender Ji (Masosa)",
        "site_area_landmark": "Masuda / Beawar",
        "rough_scope": "Full Interior (Completed & Cleared 5 Nov)",
        "pipeline_stage": "completed",
        "material_stage": "all_in_shop",
        "payment_stage": "payment_complete",
        "quoted_total": 182700.00,
        "paid_cache_inr": 182700.00,
        "metadata": {"source": "October main (Mahender Ji Masosa — marked 'all clear' on 5 Nov)"},
        "items": [
            {"item_name": "Completed Interior Woodwork", "stage": "qc_done"},
        ],
    },
]


def main() -> None:
    sb = get_supabase_admin()

    # 1. Upsert Vendors by case-insensitive canonical_name
    existing_vendors = {
        row["canonical_name"].strip().lower(): row
        for row in (sb.table("vendors").select("*").execute().data or [])
    }
    vendor_id_by_name: dict[str, str] = {}
    for v in VENDORS:
        key = v["canonical_name"].strip().lower()
        payload = {
            "canonical_name": v["canonical_name"],
            "vendor_kind": v["vendor_kind"],
            "specialty": v.get("specialty"),
            "aliases": v.get("aliases", []),
            "notes": v.get("notes"),
            "is_active": True,
        }
        if key in existing_vendors:
            vid = str(existing_vendors[key]["id"])
            sb.table("vendors").update(payload).eq("id", vid).execute()
        else:
            res = sb.table("vendors").insert(payload).execute().data
            vid = str(res[0]["id"])
            existing_vendors[key] = res[0]
        vendor_id_by_name[v["canonical_name"]] = vid

    # 2. Upsert People by case-insensitive full_name
    existing_people = {
        row["full_name"].strip().lower(): row
        for row in (sb.table("people").select("*").execute().data or [])
    }
    person_id_by_name: dict[str, str] = {
        row["full_name"]: str(row["id"]) for row in existing_people.values()
    }
    for p in PEOPLE:
        key = p["full_name"].strip().lower()
        linked_vid = (
            vendor_id_by_name.get(p["linked_vendor_name"])
            if p.get("linked_vendor_name")
            else None
        )
        if key in existing_people:
            cur = existing_people[key]
            pid = str(cur["id"])
            merged_meta = {**(cur.get("metadata") or {}), **(p.get("metadata") or {})}
            upd = {
                "person_type": p["person_type"],
                "linked_vendor_id": linked_vid or cur.get("linked_vendor_id"),
                "role_tags": p.get("role_tags", []),
                "aliases": p.get("aliases", []),
                "payout_aliases": p.get("payout_aliases", []),
                "can_login_prod": bool(cur.get("can_login_prod")) or bool(p.get("can_login_prod", False)),
                "can_login_munim": bool(cur.get("can_login_munim")) or bool(p.get("can_login_munim", False)),
                "is_default_purchase": bool(cur.get("is_default_purchase")) or bool(p.get("is_default_purchase", False)),
                "monthly_salary_inr": float(p.get("monthly_salary_inr") or cur.get("monthly_salary_inr") or 0),
                "metadata": merged_meta,
                "is_active": True,
            }
            sb.table("people").update(upd).eq("id", pid).execute()
        else:
            ins = {
                "full_name": p["full_name"],
                "person_type": p["person_type"],
                "linked_vendor_id": linked_vid,
                "role_tags": p.get("role_tags", []),
                "aliases": p.get("aliases", []),
                "payout_aliases": p.get("payout_aliases", []),
                "can_login_prod": bool(p.get("can_login_prod", False)),
                "can_login_munim": bool(p.get("can_login_munim", False)),
                "is_default_purchase": bool(p.get("is_default_purchase", False)),
                "monthly_salary_inr": float(p.get("monthly_salary_inr", 0)),
                "metadata": p.get("metadata", {}),
                "is_active": True,
            }
            res = sb.table("people").insert(ins).execute().data
            pid = str(res[0]["id"])
            existing_people[key] = res[0]
        person_id_by_name[p["full_name"]] = pid

    all_people = sb.table("people").select("*").execute().data or []
    shreyansh_id = next((str(r["id"]) for r in all_people if r.get("is_default_qc")), None)
    ansh_id = person_id_by_name.get("Ansh")

    # 3. Link Supervisor Float Custodians on `payment_accounts`
    for acct_code, custodian_name in FLOAT_CUSTODIANS.items():
        custodian_id = person_id_by_name.get(custodian_name)
        if custodian_id:
            sb.table("payment_accounts").update({"custodian_person_id": custodian_id}).eq("code", acct_code).execute()

    # 4. Upsert Customers by case-insensitive full_name
    existing_customers = {
        row["full_name"].strip().lower(): row
        for row in (sb.table("customers").select("*").execute().data or [])
    }
    customer_id_by_name: dict[str, str] = {}
    for c in CUSTOMERS:
        key = c["full_name"].strip().lower()
        payload = {
            "full_name": c["full_name"],
            "area_landmark": c.get("area_landmark"),
            "aliases": c.get("aliases", []),
            "notes": c.get("notes"),
            "is_active": True,
        }
        if key in existing_customers:
            cid = str(existing_customers[key]["id"])
            sb.table("customers").update(payload).eq("id", cid).execute()
        else:
            res = sb.table("customers").insert(payload).execute().data
            cid = str(res[0]["id"])
            existing_customers[key] = res[0]
        customer_id_by_name[c["full_name"]] = cid

    # 5. Upsert Active Execution Orders + Completed Masosa & Flat Furniture Items
    existing_projects = {
        row["project_code"]: row
        for row in (sb.table("projects").select("*").execute().data or [])
    }
    owner_ids = [shreyansh_id] if shreyansh_id else []
    for o in ORDERS:
        pcode = o["project_code"]
        cid = customer_id_by_name[o["customer_name"]]
        proj_payload = {
            "project_code": pcode,
            "cf_project_code": pcode,
            "project_type": "customer",
            "customer_id": cid,
            "customer_name": o["customer_name"],
            "site_area_landmark": o["site_area_landmark"],
            "rough_scope": o["rough_scope"],
            "pipeline_stage": o["pipeline_stage"],
            "material_stage": o["material_stage"],
            "payment_stage": o["payment_stage"],
            "gst_flag": False,
            "quoted_total": float(o["quoted_total"]),
            "paid_cache_inr": float(o["paid_cache_inr"]),
            "material_owner_id": ansh_id,
            "current_owner_ids": owner_ids,
            "metadata": {
                **((existing_projects.get(pcode) or {}).get("metadata") or {}),
                **o.get("metadata", {}),
            },
        }
        if pcode in existing_projects:
            proj_id = str(existing_projects[pcode]["id"])
            sb.table("projects").update(proj_payload).eq("id", proj_id).execute()
        else:
            res = sb.table("projects").insert(proj_payload).execute().data
            proj_id = str(res[0]["id"])
            existing_projects[pcode] = res[0]

        existing_items = {
            row["item_name"].strip().lower(): row
            for row in (
                sb.table("project_items")
                .select("*")
                .eq("project_id", proj_id)
                .execute()
                .data
                or []
            )
        }
        for item in o.get("items", []):
            ikey = item["item_name"].strip().lower()
            prod_mistri_id = (
                person_id_by_name.get(item["prod_mistri"]) if item.get("prod_mistri") else None
            )
            assy_mistri_id = (
                person_id_by_name.get(item["assy_mistri"]) if item.get("assy_mistri") else None
            )
            qc_by_id = shreyansh_id if item["stage"] == "qc_done" else None
            item_payload: dict[str, Any] = {
                "project_id": proj_id,
                "item_name": item["item_name"],
                "stage": item["stage"],
                "specs_notes": item.get("specs_notes"),
            }
            if prod_mistri_id:
                item_payload["production_mistri_id"] = prod_mistri_id
            if assy_mistri_id:
                item_payload["assembly_mistri_id"] = assy_mistri_id
            if qc_by_id:
                item_payload["qc_passed_by_id"] = qc_by_id

            if ikey in existing_items:
                sb.table("project_items").update(item_payload).eq(
                    "id", str(existing_items[ikey]["id"])
                ).execute()
            else:
                res = sb.table("project_items").insert(item_payload).execute().data
                existing_items[ikey] = res[0]

    # Live Verification Summary
    vendors_all = sb.table("vendors").select("id,vendor_kind").execute().data or []
    people_all = sb.table("people").select("id,person_type").execute().data or []
    floats_linked = (
        sb.table("payment_accounts")
        .select("id")
        .not_.is_("custodian_person_id", "null")
        .execute()
        .data
        or []
    )
    customers_all = sb.table("customers").select("id").execute().data or []
    orders_all = (
        sb.table("projects").select("id").eq("project_type", "customer").execute().data or []
    )
    items_all = sb.table("project_items").select("id").execute().data or []
    wdw_all = sb.table("v_whos_doing_what").select("work_kind").execute().data or []

    print("October Option #1 Master Seed applied cleanly!")
    print(
        f"  - Vendors: {len(vendors_all)} total "
        f"({sum(1 for v in vendors_all if v['vendor_kind']=='supplier')} suppliers, "
        f"{sum(1 for v in vendors_all if v['vendor_kind']=='contractor')} contractors)"
    )
    print(
        f"  - People: {len(people_all)} total "
        f"({sum(1 for p in people_all if p['person_type']=='operator')} operators, "
        f"{sum(1 for p in people_all if p['person_type']=='salaried_karigar')} salaried karigars, "
        f"{sum(1 for p in people_all if p['person_type']=='contractor_mistri')} contractor mistris)"
    )
    print(f"  - Supervisor Floats Linked: {len(floats_linked)} / 4")
    print(f"  - Customers Directory: {len(customers_all)}")
    print(f"  - Customer Orders: {len(orders_all)} (`PRJ-OCT-001` .. `PRJ-OCT-015`)")
    print(f"  - Flat Furniture Items: {len(items_all)}")
    print(f"  - Live `v_whos_doing_what` Queue Items: {len(wdw_all)}")


if __name__ == "__main__":
    main()
