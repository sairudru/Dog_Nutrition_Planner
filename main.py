from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import psycopg2

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FIXED_TOTAL_DM = 1000
ORGAN_DM_TARGET = 150
OIL_DM_RESERVED = 10
FRUIT_DM_LIMIT = 20
GRAIN_DM_MIN = 300
GRAIN_B_MAX = 200
GRAIN_A_MIN = 100
VEG_A_MIN = 80
VEG_B_MIN = 50
MEAT_MIN = 200
MEAT_MAX = 350
PROTEIN_MIN_PERCENT = 32

class IngredientRequest(BaseModel):
    ingredients: List[str]

def get_connection():
    return psycopg2.connect(
        host="localhost",
        database="postgres",
        user="postgres",
        password="admin"
    )

@app.get("/ingredients")
def get_ingredients():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT ingredient_name, group_name FROM user_ingredients")
    data = [{"ingredient_name": row[0], "group_name": row[1]} for row in cursor.fetchall()]
    conn.close()
    return data

@app.post("/calculate")
def calculate_diet(request: IngredientRequest):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT ingredient_name, dm_g, protein_g, fat_g, cho_g, fiber_g, ash_g, calcium_mg, phosphorus_mg, iron_mg, energy_kcal FROM fixed_ingredients")
    fixed_rows = cursor.fetchall()
    FIXED_DM_USED = sum(row[1] for row in fixed_rows)

    selected_names = list(dict.fromkeys(request.ingredients))
    ingredient_rows = []
    for name in selected_names:
        cursor.execute("""
            SELECT ingredient_name, group_name, protein_g, fat_g, cho_g, fiber_g, ash_g, calcium_mg, phosphorus_mg, iron_mg, energy_kcal
            FROM user_ingredients WHERE ingredient_name = %s
        """, (name,))
        row = cursor.fetchone()
        if row:
            ingredient_rows.append({
                "ingredient": row[0],
                "group": row[1].strip().lower(),
                "data": row[2:]
            })
    conn.close()

    dm_breakdown = []
    ingredient_totals = []
    total = {k: 0 for k in ["Protein", "Fat", "CHO", "Fiber", "Ash", "Ca", "P", "Iron", "Energy"]}
    remaining_dm = FIXED_TOTAL_DM - FIXED_DM_USED
    issues = []
    used_ingredient_names = set()

    def add_to_total(values, dm):
        keys = list(total.keys())
        for i, k in enumerate(keys):
            val = values[i]
            if k in ["Ca", "P"]:
                total[k] += (val / 1000) * dm
            elif k == "Iron":
                total[k] += (val * dm) / 100
            else:
                total[k] += (val / 100) * dm

    def add_ingredient_totals(ingredient, dm, nutrients, fixed):
        ingredient_totals.append({
            "ingredient": ingredient,
            "dm_g": dm,
            "protein_g": round(nutrients[0] * dm / 100, 2),
            "fat_g": round(nutrients[1] * dm / 100, 2),
            "cho_g": round(nutrients[2] * dm / 100, 2),
            "fiber_g": round(nutrients[3] * dm / 100, 2),
            "ash_g": round(nutrients[4] * dm / 100, 2),
            "ca_mg": round(nutrients[5] * dm / 100, 2),
            "p_mg": round(nutrients[6] * dm / 100, 2),
            "iron_mg": round(nutrients[7] * dm / 100, 2),
            "energy_kcal": round(nutrients[8] * dm / 100, 2),
            "fixed": fixed
        })

    def distribute_exact(group_list, target_dm):
        if not group_list or target_dm <= 0:
            return 0
        filtered = [r for r in group_list if r["ingredient"] not in used_ingredient_names]
        if not filtered:
            return 0
        each = round(target_dm / len(filtered), 2)
        actual_used = 0
        for i, r in enumerate(filtered):
            dm = each if i < len(filtered) - 1 else target_dm - each * (len(filtered) - 1)
            add_to_total(r["data"], dm)
            dm_breakdown.append({"ingredient": r["ingredient"], "dm_g": dm, "fixed": False})
            add_ingredient_totals(r["ingredient"], dm, r["data"], fixed=False)
            used_ingredient_names.add(r["ingredient"])
            actual_used += dm
        return actual_used

    for row in fixed_rows:
        name, dm, *nutrients = row
        add_to_total(nutrients, dm)
        dm_breakdown.append({"ingredient": name, "dm_g": dm, "fixed": True})
        add_ingredient_totals(name, dm, nutrients, fixed=True)
        used_ingredient_names.add(name)

    organ_meats = [r for r in ingredient_rows if "organ" in r["group"]]
    liver = [r for r in organ_meats if "liver" in r["ingredient"].lower()]
    other_organs = [r for r in organ_meats if r not in liver]
    if liver:
        liver_dm = int(ORGAN_DM_TARGET * 2 / 3)
        liver_used = distribute_exact(liver[:1], liver_dm)
        remaining_dm -= liver_used
        max_other_dm = ORGAN_DM_TARGET - liver_used
        other_used = distribute_exact(other_organs, max_other_dm)
        remaining_dm -= other_used
    else:
        issues.append("Liver is required (10% of DM).")

    veg_a = [r for r in ingredient_rows if r["group"] == "vegetable a"]
    veg_b = [r for r in ingredient_rows if r["group"] == "vegetable b"]
    veg_c = [r for r in ingredient_rows if r["group"] == "vegetable c"]
    veg_a_used = distribute_exact(veg_a, VEG_A_MIN)
    veg_b_used = distribute_exact(veg_b, VEG_B_MIN)
    veg_c_target = 150 - veg_a_used - veg_b_used
    veg_c_used = distribute_exact(veg_c, max(0, veg_c_target))
    remaining_dm -= (veg_a_used + veg_b_used + veg_c_used)

    remaining_dm -= distribute_exact([r for r in ingredient_rows if "fruit" in r["group"]], FRUIT_DM_LIMIT)
    remaining_dm -= distribute_exact([r for r in ingredient_rows if "oil" in r["group"]], OIL_DM_RESERVED)

    grain_a = [r for r in ingredient_rows if r["group"] == "grain a"]
    grain_b = [r for r in ingredient_rows if r["group"] == "grain b"]
    remaining_dm -= distribute_exact(grain_a, GRAIN_A_MIN)
    remaining_dm -= distribute_exact(grain_b, GRAIN_B_MAX)

    meat_a = [r for r in ingredient_rows if r["group"] == "meat group a"]
    meat_b = [r for r in ingredient_rows if r["group"] == "meat group b"]
    meat_c = [r for r in ingredient_rows if r["group"] == "meat group c"]
    meat_used = 0

    if meat_a and not meat_b and not meat_c:
        avg_fat = sum(r["data"][1] for r in meat_a) / len(meat_a)
        if avg_fat < 12:
            supplement = [r for r in ingredient_rows if "meat group b" in r["group"] or "meat group c" in r["group"]]
            supplement = sorted(supplement, key=lambda x: -x["data"][1])
            meat_used += distribute_exact(meat_a, 150)
            meat_used += distribute_exact(supplement, MEAT_MIN - meat_used)
        else:
            meat_used += distribute_exact(meat_a, MEAT_MAX)

    elif meat_b and not meat_a:
        avg_fat = sum(r["data"][1] for r in meat_b) / len(meat_b)
        if avg_fat > 30:
            meat_used += distribute_exact(meat_b, 200)
            fallback = [r for r in ingredient_rows if "meat group a" in r["group"] and r["ingredient"] not in used_ingredient_names]
            meat_used += distribute_exact(fallback, 150)
        else:
            meat_used += distribute_exact(meat_b, MEAT_MAX)

    elif meat_c and not meat_a and not meat_b:
        avg_fat = sum(r["data"][1] for r in meat_c) / len(meat_c)
        if avg_fat > 16:
            meat_used += distribute_exact(meat_c, 200)
            fallback = [r for r in ingredient_rows if "meat group a" in r["group"] and r["ingredient"] not in used_ingredient_names]
            meat_used += distribute_exact(fallback, 100)
        else:
            meat_used += distribute_exact(meat_c, MEAT_MAX)

    else:
        all_meats = meat_a + meat_b + meat_c
        meat_used += distribute_exact(all_meats, MEAT_MAX)

    if remaining_dm > 0:
        remaining_group = [r for r in ingredient_rows if r["ingredient"] not in used_ingredient_names]
        remaining_dm -= distribute_exact(remaining_group, remaining_dm)

    total_dm_used = sum([item["dm_g"] for item in dm_breakdown])
    scaling_needed = FIXED_TOTAL_DM - total_dm_used
    non_fixed = [item for item in ingredient_totals if not item["fixed"]]
    non_fixed_total = sum(item["dm_g"] for item in non_fixed)
    if non_fixed_total > 0 and abs(scaling_needed) >= 0.1:
        scale_factor = (non_fixed_total + scaling_needed) / non_fixed_total
        for item in ingredient_totals:
            if not item["fixed"]:
                old_dm = item["dm_g"]
                new_dm = round(old_dm * scale_factor, 2)
                for nutrient in ["protein_g", "fat_g", "cho_g", "fiber_g", "ash_g", "ca_mg", "p_mg", "iron_mg", "energy_kcal"]:
                    item[nutrient] = round(item[nutrient] * scale_factor, 2)
                item["dm_g"] = new_dm
                for db in dm_breakdown:
                    if db["ingredient"] == item["ingredient"] and not db["fixed"]:
                        db["dm_g"] = new_dm
                        break

    total = {k: 0 for k in total}
    for item in ingredient_totals:
        dm = item["dm_g"]
        total["Protein"] += item["protein_g"]
        total["Fat"] += item["fat_g"]
        total["CHO"] += item["cho_g"]
        total["Fiber"] += item["fiber_g"]
        total["Ash"] += item["ash_g"]
        total["Ca"] += item["ca_mg"] / 1000
        total["P"] += item["p_mg"] / 1000
        total["Iron"] += item["iron_mg"] / 100
        total["Energy"] += item["energy_kcal"]

    if total["Protein"] * 100 / FIXED_TOTAL_DM < PROTEIN_MIN_PERCENT:
        needed_protein_g = (PROTEIN_MIN_PERCENT * FIXED_TOTAL_DM / 100) - total["Protein"]
        boost_meats = sorted(
            [r for r in ingredient_rows if r["group"] in ["meat group a", "meat group b", "meat group c"] and r["ingredient"] not in used_ingredient_names],
            key=lambda x: -x["data"][0]
        )
        for r in boost_meats:
            protein_per_g = r["data"][0] / 100
            if protein_per_g > 0:
                dm_needed = min(remaining_dm, round(needed_protein_g / protein_per_g, 2))
                added_dm = distribute_exact([r], dm_needed)
                remaining_dm -= added_dm
                total["Protein"] += added_dm * protein_per_g
                if total["Protein"] * 100 / FIXED_TOTAL_DM >= PROTEIN_MIN_PERCENT:
                    break
    # Fat Correction Logic
            # Fat rule: Ensure fat% is between 12% and 17%
    FAT_MIN_PERCENT = 12
    FAT_MAX_PERCENT = 17

    if fat_percent > FAT_MAX_PERCENT:
        excess_fat = fat_percent - FAT_MAX_PERCENT
        meat_b_items = [r for r in ingredient_rows if r.get("group") == "meat group b"]

        for item in meat_b_items:
            dm_g = item.get("dm_g")
            if dm_g is None:
                dm_g = 100 - item.get("water_g", 0)

            fat_g = item.get("fat_g", 0)
            fat_per_g = fat_g / dm_g if dm_g else 0

            if fat_per_g > 0:
                reducible_dm = round(((excess_fat / 100) * FIXED_TOTAL_DM) / fat_per_g, 2)
                new_dm = max(0, dm_g - reducible_dm)

            # Recalculate nutrient values
                scale_factor = new_dm / dm_g if dm_g else 1
                for nutrient in ["protein_g", "fat_g", "cho_g", "fiber_g", "ash_g", "ca_mg", "p_mg", "iron_mg", "energy_kcal"]:
                    if nutrient in item:
                        item[nutrient] = round(item[nutrient] * scale_factor, 2)

                item["dm_g"] = new_dm

            # Update dm_breakdown
                for db in dm_breakdown:
                    if db["ingredient"] == item["ingredient"] and not db.get("fixed", False):
                        db["dm_g"] = new_dm
                        break

            # Update total fat safely
                total["Fat"] -= fat_g  # fat_g already set safely above
                fat_percent = total["Fat"] * 100 / FIXED_TOTAL_DM

                if fat_percent <= FAT_MAX_PERCENT:
                    break


    if fat_percent < FAT_MIN_PERCENT:
        additional_fat_needed = FAT_MIN_PERCENT - fat_percent
        added_fat = 0

        oil_order = ["coconut oil", "wheatgerm oil", "sunflower oil", "fish oil"]  # adjust priority as needed

        for oil in oil_order:
            oil_entry = next((i for i in ingredient_totals if i["ingredient"].lower() == oil.lower()), None)
            if oil_entry:
                dm_to_add = 10  # 10g DM

                oil_entry["dm_g"] += dm_to_add
                for nutrient in ["protein_g", "fat_g", "cho_g", "fiber_g", "ash_g", "ca_mg", "p_mg", "iron_mg", "energy_kcal"]:
                    per_g = oil_entry[nutrient] / (oil_entry["dm_g"] - dm_to_add) if (oil_entry["dm_g"] - dm_to_add) > 0 else 0
                    oil_entry[nutrient] += round(per_g * dm_to_add, 2)

                total["Fat"] += oil_entry["fat_g"]
                fat_percent = total["Fat"] * 100 / FIXED_TOTAL_DM

            if fat_percent >= FAT_MIN_PERCENT:
                break
   
    


    result = {
        "Protein_percent": round(total["Protein"] * 100 / FIXED_TOTAL_DM, 2),
        "Fat_percent": round(total["Fat"] * 100 / FIXED_TOTAL_DM, 2),
        "CHO_percent": round(total["CHO"] * 100 / FIXED_TOTAL_DM, 2),
        "Fiber_percent": round(total["Fiber"] * 100 / FIXED_TOTAL_DM, 2),
        "Ash_percent": round(total["Ash"] * 100 / FIXED_TOTAL_DM, 2),
        "Ca_percent": round(total["Ca"] * 100 / FIXED_TOTAL_DM, 2),
        "P_percent": round(total["P"] * 100 / FIXED_TOTAL_DM, 2),
        "Ca_P_ratio": round(total["Ca"] / total["P"], 2) if total["P"] else 0,
        "Energy": round(total["Energy"], 2),
        "DM_percent": round(FIXED_TOTAL_DM, 2)
    }

    return {
        "nutrient_percentages": result,
        "dm_breakdown": dm_breakdown,
        "ingredient_totals": ingredient_totals,
        
        "issues": issues,
        "auto_added": None
    }