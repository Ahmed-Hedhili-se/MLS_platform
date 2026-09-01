import nbformat as nbf

nb = nbf.v4.new_notebook()

cells = [
    """
import pandas as pd
import numpy as np
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler

df = pd.read_csv("employee_data.csv")
""",
    """
exp_median = df["YearsExperience"].median()
perf_median = df["PerformanceScore"].median()
df["YearsExperience"] = df["YearsExperience"].fillna(exp_median)
df["PerformanceScore"] = df["PerformanceScore"].fillna(perf_median)
""",
    """
cat_cols = ["Department", "EducationLevel", "RemoteStatus"]
df_encoded = pd.get_dummies(df, columns=cat_cols, drop_first=True)
""",
    """
q1 = df_encoded["Salary"].quantile(0.25)
q3 = df_encoded["Salary"].quantile(0.75)
iqr = q3 - q1
lower_bound = q1 - 1.5 * iqr
upper_bound = q3 + 1.5 * iqr
df_clean = df_encoded[(df_encoded["Salary"] >= lower_bound) & (df_encoded["Salary"] <= upper_bound)].reset_index(drop=True)
""",
    """
feature_cols = [c for c in df_clean.columns if c not in ("Salary", "Attrition")]
X = df_clean[feature_cols].to_numpy()
y = df_clean["Salary"].to_numpy()
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=1)
""",
    """
scaler = StandardScaler()
X_train_scaled = scaler.fit_transform(X_train)
X_test_scaled = scaler.transform(X_test)
""",
]

for source in cells:
    nb.cells.append(nbf.v4.new_code_cell(source.strip()))

with open("preprocessing_lab.ipynb", "w") as f:
    nbf.write(nb, f)

print("Built preprocessing_lab.ipynb")
