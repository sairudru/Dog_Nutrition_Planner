from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict
import psycopg2
from psycopg2.pool import SimpleConnectionPool
from contextlib import contextmanager
from pydantic_settings import BaseSettings
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration settings
class Settings(BaseSettings):
    db_host: str = "localhost"
    db_name: str = "postgres"
    db_user: str = "postgres"
    db_password: str = "admin"
    allowed_origins: List[str] = ["http://localhost:3000", "http://localhost:3001"]
    
    class Config:
        env_file = ".env"

settings = Settings()

# Nutrient constraints configuration
class NutrientConstraints(BaseModel):
    fixed_total_dm: float = 1000
    organ_dm_target: float = 150
    oil_dm_reserved: float = 10
    fruit_dm_limit: float = 20
    grain_dm_min: float = 300
    grain_b_max: float = 200
    grain_a_min: float = 100
    veg_a_min: float = 80
    veg_b_min: float = 50
    meat_min: float = 200
    meat_max: float = 350
    protein_min_percent: float = 32
    fat_min_percent: float = 12
    fat_max_percent: float = 17

constraints = NutrientConstraints()

# Database connection pool
pool = SimpleConnectionPool(
    minconn=1,
    maxconn=10,
    host=settings.db_host,
    database=settings.db_name,
    user=settings.db_user,
    password=settings.db_password
)

@contextmanager
def get_cursor():
    conn = pool.getconn()
    try:
        cursor = conn.cursor()
        yield cursor
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Database error: {str(e)}")
        raise HTTPException(status_code=500, detail="Database operation failed")
    finally:
        pool.putconn(conn)

# Request/Response models
class IngredientRequest(BaseModel):
    ingredients: List[str]

class NutrientResult(BaseModel):
    protein_percent: float
    fat_percent: float
    cho_percent: float
    fiber_percent: float
    ash_percent: float
    ca_percent: float
    p_percent: float
    ca_p_ratio: float
    energy: float
    dm_percent: float

class IngredientBreakdown(BaseModel):
    ingredient: str
    dm_g: float
    fixed: bool

class IngredientTotals(BaseModel):
    ingredient: str
    dm_g: float
    protein_g: float
    fat_g: float
    cho_g: float
    fiber_g: float
    ash_g: float
    ca_mg: float
    p_mg: float
    iron_mg: float
    energy_kcal: float
    fixed: bool

class DietResult(BaseModel):
    nutrient_percentages: NutrientResult
    dm_breakdown: List[IngredientBreakdown]
    ingredient_totals: List[IngredientTotals]
    issues: List[str]
    warnings: List[str]
    suggestions: List[str]

# FastAPI app setup
app = FastAPI(
    title="Diet Formulation API",
    description="API for calculating nutritionally balanced diets",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Diet Calculator Service
class DietCalculator:
    def __init__(self, constraints: NutrientConstraints):
        self.constraints = constraints
        self.total = {k: 0 for k in ["Protein", "Fat", "CHO", "Fiber", "Ash", "Ca", "P", "Iron", "Energy"]}
        self.dm_breakdown = []
        self.ingredient_totals = []
        self.used_ingredient_names = set()
        self.issues = []
        self.warnings = []
        self.suggestions = []

    def add_to_total(self, values, dm):
        keys = list(self.total.keys())
        for i, k in enumerate(keys):
            val = values[i]
            if k in ["Ca", "P"]:
                self.total[k] += (val / 1000) * dm
            elif k == "Iron":
                self.total[k] += (val * dm) / 100
            else:
                self.total[k] += (val / 100) * dm

    def add_ingredient_totals(self, ingredient, dm, nutrients, fixed):
        self.ingredient_totals.append({
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

    def distribute_exact(self, group_list, target_dm):
        if not group_list or target_dm <= 0:
            return 0
        
        filtered = [r for r in group_list if r["ingredient"] not in self.used_ingredient_names]
        if not filtered:
            return 0
            
        each = round(target_dm / len(filtered), 2)
        actual_used = 0
        
        for i, r in enumerate(filtered):
            dm = each if i < len(filtered) - 1 else target_dm - each * (len(filtered) - 1)
            self.add_to_total(r["data"], dm)
            self.dm_breakdown.append({"ingredient": r["ingredient"], "dm_g": dm, "fixed": False})
            self.add_ingredient_totals(r["ingredient"], dm, r["data"], fixed=False)
            self.used_ingredient_names.add(r["ingredient"])
            actual_used += dm
            
        return actual_used

    def scale_ingredients(self, scaling_needed):
        non_fixed = [item for item in self.ingredient_totals if not item["fixed"]]
        non_fixed_total = sum(item["dm_g"] for item in non_fixed)
        
        if non_fixed_total > 0 and abs(scaling_needed) >= 0.1:
            scale_factor = (non_fixed_total + scaling_needed) / non_fixed_total
            for item in self.ingredient_totals:
                if not item["fixed"]:
                    old_dm = item["dm_g"]
                    new_dm = round(old_dm * scale_factor, 2)
                    for nutrient in ["protein_g", "fat_g", "cho_g", "fiber_g", "ash_g", 
                                   "ca_mg", "p_mg", "iron_mg", "energy_kcal"]:
                        item[nutrient] = round(item[nutrient] * scale_factor, 2)
                    item["dm_g"] = new_dm
                    
                    for db in self.dm_breakdown:
                        if db["ingredient"] == item["ingredient"] and not db["fixed"]:
                            db["dm_g"] = new_dm
                            break

    def calculate_nutrient_percentages(self):
    # Recalculate totals after all adjustments
        self.total = {k: 0 for k in self.total}
        for item in self.ingredient_totals:
            dm = item["dm_g"]
            self.total["Protein"] += item["protein_g"]
            self.total["Fat"] += item["fat_g"]
            self.total["CHO"] += item["cho_g"]
            self.total["Fiber"] += item["fiber_g"]
            self.total["Ash"] += item["ash_g"]
            self.total["Ca"] += item["ca_mg"] / 1000
            self.total["P"] += item["p_mg"] / 1000
            self.total["Iron"] += item["iron_mg"] / 100
            self.total["Energy"] += item["energy_kcal"]

        return {
        "protein_percent": round(self.total["Protein"] * 100 / self.constraints.fixed_total_dm, 2),
        "fat_percent": round(self.total["Fat"] * 100 / self.constraints.fixed_total_dm, 2),
        "cho_percent": round(self.total["CHO"] * 100 / self.constraints.fixed_total_dm, 2),
        "fiber_percent": round(self.total["Fiber"] * 100 / self.constraints.fixed_total_dm, 2),
        "ash_percent": round(self.total["Ash"] * 100 / self.constraints.fixed_total_dm, 2),
        "ca_percent": round(self.total["Ca"] * 100 / self.constraints.fixed_total_dm, 2),
        "p_percent": round(self.total["P"] * 100 / self.constraints.fixed_total_dm, 2),
        "ca_p_ratio": round(self.total["Ca"] / self.total["P"], 2) if self.total["P"] else 0,
        "energy": round(self.total["Energy"], 2),
        "dm_percent": round(self.constraints.fixed_total_dm, 2)
    }  

    def adjust_protein(self, ingredient_rows, remaining_dm):
        protein_percent = self.total["Protein"] * 100 / self.constraints.fixed_total_dm
        if protein_percent < self.constraints.protein_min_percent:
            needed_protein_g = (self.constraints.protein_min_percent * self.constraints.fixed_total_dm / 100) - self.total["Protein"]
            boost_meats = sorted(
                [r for r in ingredient_rows if r["group"] in ["meat group a", "meat group b", "meat group c"] 
                 and r["ingredient"] not in self.used_ingredient_names],
                key=lambda x: -x["data"][0]
            )
            for r in boost_meats:
                protein_per_g = r["data"][0] / 100
                if protein_per_g > 0:
                    dm_needed = min(remaining_dm, round(needed_protein_g / protein_per_g, 2))
                    added_dm = self.distribute_exact([r], dm_needed)
                    remaining_dm -= added_dm
                    self.total["Protein"] += added_dm * protein_per_g
                    if self.total["Protein"] * 100 / self.constraints.fixed_total_dm >= self.constraints.protein_min_percent:
                        break

    def adjust_fat(self, ingredient_rows, ingredient_totals):
        fat_percent = self.total['Fat'] * 100 / self.constraints.fixed_total_dm
        
        # Reduce fat if over max
        if fat_percent > self.constraints.fat_max_percent:
            excess_fat = fat_percent - self.constraints.fat_max_percent
            meat_b_items = [r for r in ingredient_rows if r.get("group") == "meat group b"]

            for item in meat_b_items:
                dm_g = item.get("dm_g", 100)  # Default to 100 if not set
                fat_g = item.get("fat_g", 0)
                fat_per_g = fat_g / dm_g if dm_g else 0

                if fat_per_g > 0:
                    reducible_dm = round(((excess_fat / 100) * self.constraints.fixed_total_dm) / fat_per_g, 2)
                    new_dm = max(0, dm_g - reducible_dm)

                    # Update totals
                    fat_reduction = fat_per_g * reducible_dm
                    self.total["Fat"] -= fat_reduction
                    fat_percent = self.total["Fat"] * 100 / self.constraints.fixed_total_dm

                    if fat_percent <= self.constraints.fat_max_percent:
                        break

        # Add fat if under min
        if fat_percent < self.constraints.fat_min_percent:
            additional_fat_needed = self.constraints.fat_min_percent - fat_percent
            oil_order = ["coconut oil", "wheatgerm oil", "sunflower oil", "fish oil"]

            for oil in oil_order:
                oil_entry = next((i for i in ingredient_totals if i["ingredient"].lower() == oil.lower()), None)
                if oil_entry:
                    dm_to_add = 10  # 10g DM
                    oil_fat_percent = oil_entry["fat_g"] / oil_entry["dm_g"] if oil_entry["dm_g"] > 0 else 0
                    
                    oil_entry["dm_g"] += dm_to_add
                    oil_entry["fat_g"] += round(oil_fat_percent * dm_to_add, 2)
                    self.total["Fat"] += oil_fat_percent * dm_to_add
                    fat_percent = self.total["Fat"] * 100 / self.constraints.fixed_total_dm

                    if fat_percent >= self.constraints.fat_min_percent:
                        break

    def calculate(self, ingredient_rows, fixed_rows):
        # Process fixed ingredients
        fixed_dm_used = sum(row[1] for row in fixed_rows)
        remaining_dm = self.constraints.fixed_total_dm - fixed_dm_used

        for row in fixed_rows:
            name, dm, *nutrients = row
            self.add_to_total(nutrients, dm)
            self.dm_breakdown.append({"ingredient": name, "dm_g": dm, "fixed": True})
            self.add_ingredient_totals(name, dm, nutrients, fixed=True)
            self.used_ingredient_names.add(name)

        # Process organ meats
        organ_meats = [r for r in ingredient_rows if "organ" in r["group"]]
        liver = [r for r in organ_meats if "liver" in r["ingredient"].lower()]
        other_organs = [r for r in organ_meats if r not in liver]
        
        if liver:
            liver_dm = int(self.constraints.organ_dm_target * 2 / 3)
            liver_used = self.distribute_exact(liver[:1], liver_dm)
            remaining_dm -= liver_used
            max_other_dm = self.constraints.organ_dm_target - liver_used
            other_used = self.distribute_exact(other_organs, max_other_dm)
            remaining_dm -= other_used
        else:
            self.issues.append("Liver is required (10% of DM).")

        # Process vegetables
        veg_a = [r for r in ingredient_rows if r["group"] == "vegetable a"]
        veg_b = [r for r in ingredient_rows if r["group"] == "vegetable b"]
        veg_c = [r for r in ingredient_rows if r["group"] == "vegetable c"]
        
        veg_a_used = self.distribute_exact(veg_a, self.constraints.veg_a_min)
        veg_b_used = self.distribute_exact(veg_b, self.constraints.veg_b_min)
        veg_c_target = 150 - veg_a_used - veg_b_used
        veg_c_used = self.distribute_exact(veg_c, max(0, veg_c_target))
        remaining_dm -= (veg_a_used + veg_b_used + veg_c_used)

        # Process fruits and oils
        remaining_dm -= self.distribute_exact(
            [r for r in ingredient_rows if "fruit" in r["group"]], 
            self.constraints.fruit_dm_limit
        )
        remaining_dm -= self.distribute_exact(
            [r for r in ingredient_rows if "oil" in r["group"]], 
            self.constraints.oil_dm_reserved
        )

        # Process grains
        grain_a = [r for r in ingredient_rows if r["group"] == "grain a"]
        grain_b = [r for r in ingredient_rows if r["group"] == "grain b"]
        remaining_dm -= self.distribute_exact(grain_a, self.constraints.grain_a_min)
        remaining_dm -= self.distribute_exact(grain_b, self.constraints.grain_b_max)

        # Process meats
        meat_a = [r for r in ingredient_rows if r["group"] == "meat group a"]
        meat_b = [r for r in ingredient_rows if r["group"] == "meat group b"]
        meat_c = [r for r in ingredient_rows if r["group"] == "meat group c"]
        meat_used = 0

        if meat_a and not meat_b and not meat_c:
            avg_fat = sum(r["data"][1] for r in meat_a) / len(meat_a)
            if avg_fat < 12:
                supplement = [r for r in ingredient_rows if "meat group b" in r["group"] or "meat group c" in r["group"]]
                supplement = sorted(supplement, key=lambda x: -x["data"][1])
                meat_used += self.distribute_exact(meat_a, 150)
                meat_used += self.distribute_exact(supplement, self.constraints.meat_min - meat_used)
            else:
                meat_used += self.distribute_exact(meat_a, self.constraints.meat_max)

        elif meat_b and not meat_a:
            avg_fat = sum(r["data"][1] for r in meat_b) / len(meat_b)
            if avg_fat > 30:
                meat_used += self.distribute_exact(meat_b, 200)
                fallback = [r for r in ingredient_rows if "meat group a" in r["group"] and r["ingredient"] not in self.used_ingredient_names]
                meat_used += self.distribute_exact(fallback, 150)
            else:
                meat_used += self.distribute_exact(meat_b, self.constraints.meat_max)

        elif meat_c and not meat_a and not meat_b:
            avg_fat = sum(r["data"][1] for r in meat_c) / len(meat_c)
            if avg_fat > 16:
                meat_used += self.distribute_exact(meat_c, 200)
                fallback = [r for r in ingredient_rows if "meat group a" in r["group"] and r["ingredient"] not in self.used_ingredient_names]
                meat_used += self.distribute_exact(fallback, 100)
            else:
                meat_used += self.distribute_exact(meat_c, self.constraints.meat_max)
        else:
            all_meats = meat_a + meat_b + meat_c
            meat_used += self.distribute_exact(all_meats, self.constraints.meat_max)

        # Distribute remaining DM
        if remaining_dm > 0:
            remaining_group = [r for r in ingredient_rows if r["ingredient"] not in self.used_ingredient_names]
            remaining_dm -= self.distribute_exact(remaining_group, remaining_dm)

        # Scale ingredients to match total DM
        total_dm_used = sum(item["dm_g"] for item in self.dm_breakdown)
        scaling_needed = self.constraints.fixed_total_dm - total_dm_used
        self.scale_ingredients(scaling_needed)

        # Adjust protein and fat
        self.adjust_protein(ingredient_rows, remaining_dm)
        self.adjust_fat(ingredient_rows, self.ingredient_totals)

        # Calculate final nutrient percentages
        nutrient_percentages = self.calculate_nutrient_percentages()

        return {
            "nutrient_percentages": nutrient_percentages,
            "dm_breakdown": self.dm_breakdown,
            "ingredient_totals": self.ingredient_totals,
            "issues": self.issues,
            "warnings": self.warnings,
            "suggestions": self.suggestions
        }

# API Endpoints
@app.get("/ingredients", summary="Get all available ingredients")
async def get_ingredients():
    """Retrieve a list of all available ingredients with their groups"""
    try:
        with get_cursor() as cursor:
            cursor.execute("SELECT ingredient_name, group_name FROM user_ingredients")
            data = [{"ingredient_name": row[0], "group_name": row[1]} for row in cursor.fetchall()]
            return data
    except Exception as e:
        logger.error(f"Failed to fetch ingredients: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch ingredients")

@app.post("/calculate", 
          response_model=DietResult,
          summary="Calculate balanced diet",
          description="Calculates a nutritionally balanced diet based on provided ingredients")
async def calculate_diet(request: IngredientRequest):
    """
    Calculate a balanced diet formulation.
    
    Args:
        request: IngredientRequest containing list of ingredient names
        
    Returns:
        Diet formulation with nutrient percentages and ingredient breakdown
    """
    try:
        with get_cursor() as cursor:
            # Get fixed ingredients
            cursor.execute("""
                SELECT ingredient_name, dm_g, protein_g, fat_g, cho_g, fiber_g, ash_g, 
                       calcium_mg, phosphorus_mg, iron_mg, energy_kcal 
                FROM fixed_ingredients
            """)
            fixed_rows = cursor.fetchall()

            # Get selected ingredients
            selected_names = list(dict.fromkeys(request.ingredients))
            ingredient_rows = []
            for name in selected_names:
                cursor.execute("""
                    SELECT ingredient_name, group_name, protein_g, fat_g, cho_g, fiber_g, 
                           ash_g, calcium_mg, phosphorus_mg, iron_mg, energy_kcal
                    FROM user_ingredients WHERE ingredient_name = %s
                """, (name,))
                row = cursor.fetchone()
                if row:
                    ingredient_rows.append({
                        "ingredient": row[0],
                        "group": row[1].strip().lower(),
                        "data": row[2:]
                    })

        if not ingredient_rows:
            raise HTTPException(status_code=400, detail="No valid ingredients provided")

        calculator = DietCalculator(constraints)
        result = calculator.calculate(ingredient_rows, fixed_rows)
        
        return result
    except ResponseValidationError as e:
        logger.error(f"Response validation failed: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail="Internal server error: Response validation failed"
        )
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Diet calculation failed: {str(e)}")
        raise HTTPException(status_code=500, detail="Diet calculation failed")

# Startup event
@app.on_event("startup")
async def startup():
    logger.info("Starting up Diet Formulation API")

# Shutdown event
@app.on_event("shutdown")
async def shutdown():
    pool.closeall()
    logger.info("Shutting down Diet Formulation API")