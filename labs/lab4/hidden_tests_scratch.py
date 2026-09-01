"""
Hidden tests for the KNN scratch implementation.

This file must NEVER be included in student repositories.

The tests verify the required behavior of the student's implementation,
not the exact implementation strategy.
"""

import numpy as np


# ============================================================
# Helpers
# ============================================================

def _passed():
    print("\033[92mAll tests passed!\033[0m")


def _fail(message):
    raise AssertionError(message)


# ============================================================
# 1. Train / Validation / Test Split
# ============================================================

def split_test(X_train, X_val, X_test, y_train, y_val, y_test):
    """Verify an approximately 60 / 20 / 20 split."""

    X_train = np.asarray(X_train)
    X_val = np.asarray(X_val)
    X_test = np.asarray(X_test)

    y_train = np.asarray(y_train)
    y_val = np.asarray(y_val)
    y_test = np.asarray(y_test)

    if X_train.ndim != 2 or X_val.ndim != 2 or X_test.ndim != 2:
        _fail("All feature splits must be 2D.")

    if y_train.ndim != 1 or y_val.ndim != 1 or y_test.ndim != 1:
        _fail("All target splits must be 1D.")

    for X_part, y_part, name in [
        (X_train, y_train, "train"),
        (X_val, y_val, "validation"),
        (X_test, y_test, "test"),
    ]:
        if len(X_part) != len(y_part):
            _fail(f"{name} X and y have different sample counts.")

    if X_train.shape[1] != X_val.shape[1] != X_test.shape[1]:
        _fail("All feature splits must have the same number of features.")

    total = len(X_train) + len(X_val) + len(X_test)
    if total == 0:
        _fail("The splits must not be empty.")

    ratios = (
        len(X_train) / total,
        len(X_val) / total,
        len(X_test) / total,
    )

    expected = (0.60, 0.20, 0.20)

    for actual, target, name in zip(
        ratios,
        expected,
        ["training", "validation", "test"],
    ):
        if not np.isclose(actual, target, atol=0.02):
            _fail(
                f"{name} split should be approximately {target:.0%}; "
                f"got {actual:.3f}."
            )

    _passed()



# ============================================================
# 2. Euclidean Distance
# ============================================================

def euclidean_distance_test(distance):
    """Test pairwise Euclidean distance."""

    X1 = np.array([
        [0.0, 0.0],
        [3.0, 4.0],
    ])

    X2 = np.array([
        [0.0, 0.0],
        [6.0, 8.0],
    ])

    result = distance(
        X1,
        X2,
        metric="euclidean",
    )

    expected = np.array([
        [0.0, 10.0],
        [5.0, 5.0],
    ])

    if not isinstance(result, np.ndarray):
        _fail("distance() must return a NumPy array.")

    if result.shape != (2, 2):
        _fail(
            f"Expected shape (2, 2), got {result.shape}."
        )

    if not np.allclose(result, expected, atol=1e-8):
        _fail(
            "Incorrect Euclidean distances.\n"
            f"Expected:\n{expected}\n"
            f"Got:\n{result}"
        )

    _passed()


# ============================================================
# 3. Manhattan Distance
# ============================================================

def manhattan_distance_test(distance):
    """Test pairwise Manhattan distance."""

    X1 = np.array([
        [0.0, 0.0],
        [3.0, 4.0],
    ])

    X2 = np.array([
        [0.0, 0.0],
        [6.0, 8.0],
    ])

    result = distance(
        X1,
        X2,
        metric="manhattan",
    )

    expected = np.array([
        [0.0, 14.0],
        [7.0, 7.0],
    ])

    if not isinstance(result, np.ndarray):
        _fail("Manhattan distance must return a NumPy array.")

    if result.shape != (2, 2):
        _fail(
            f"Expected shape (2, 2), got {result.shape}."
        )

    if not np.allclose(result, expected, atol=1e-8):
        _fail(
            "Incorrect Manhattan distances.\n"
            f"Expected:\n{expected}\n"
            f"Got:\n{result}"
        )

    _passed()


# ============================================================
# 4. Minkowski Distance
# ============================================================

def minkowski_distance_test(distance):
    """Test Minkowski distance and its relationship to other metrics."""

    X1 = np.array([
        [-1.0, -2.0],
        [-3.0, 4.0],
    ])

    X2 = np.array([
        [2.0, 1.0],
        [-5.0, -1.0],
    ])

    # --------------------------------------------------------
    # Default p = 3
    # --------------------------------------------------------

    result = distance(
        X1,
        X2,
        metric="minkowski",
    )

    diff = X1[:, None, :] - X2[None, :, :]

    expected = (
        np.sum(np.abs(diff) ** 3, axis=2)
        ** (1 / 3)
    )

    if not isinstance(result, np.ndarray):
        _fail("Minkowski distance must return a NumPy array.")

    if result.shape != (2, 2):
        _fail(
            f"Expected shape (2, 2), got {result.shape}."
        )

    if not np.allclose(result, expected, atol=1e-8):
        _fail(
            "Incorrect Minkowski distance for default p=3."
        )

    # --------------------------------------------------------
    # Non-integer p
    # --------------------------------------------------------

    result = distance(
        X1,
        X2,
        metric="minkowski",
        p=1.5,
    )

    expected = (
        np.sum(np.abs(diff) ** 1.5, axis=2)
        ** (1 / 1.5)
    )

    if not np.allclose(result, expected, atol=1e-8):
        _fail(
            "Incorrect Minkowski distance for p=1.5."
        )

    # --------------------------------------------------------
    # p = 1 == Manhattan
    # --------------------------------------------------------

    minkowski_p1 = distance(
        X1,
        X2,
        metric="minkowski",
        p=1,
    )

    manhattan = distance(
        X1,
        X2,
        metric="manhattan",
    )

    if not np.allclose(
        minkowski_p1,
        manhattan,
        atol=1e-8,
    ):
        _fail(
            "Minkowski with p=1 must equal Manhattan distance."
        )

    # --------------------------------------------------------
    # p = 2 == Euclidean
    # --------------------------------------------------------

    minkowski_p2 = distance(
        X1,
        X2,
        metric="minkowski",
        p=2,
    )

    euclidean = distance(
        X1,
        X2,
        metric="euclidean",
    )

    if not np.allclose(
        minkowski_p2,
        euclidean,
        atol=1e-8,
    ):
        _fail(
            "Minkowski with p=2 must equal Euclidean distance."
        )

    _passed()


# ============================================================
# 5. Chebyshev Distance
# ============================================================

def chebyshev_distance_test(distance):
    """Test pairwise Chebyshev distance."""

    X1 = np.array([
        [0.0, 0.0],
        [3.0, 4.0],
    ])

    X2 = np.array([
        [0.0, 0.0],
        [6.0, 8.0],
    ])

    result = distance(
        X1,
        X2,
        metric="chebyshev",
    )

    expected = np.array([
        [0.0, 8.0],
        [4.0, 4.0],
    ])

    if not isinstance(result, np.ndarray):
        _fail("Chebyshev distance must return a NumPy array.")

    if result.shape != (2, 2):
        _fail(
            f"Expected shape (2, 2), got {result.shape}."
        )

    if not np.allclose(result, expected, atol=1e-8):
        _fail(
            "Incorrect Chebyshev distances.\n"
            f"Expected:\n{expected}\n"
            f"Got:\n{result}"
        )

    _passed()


# ============================================================
# 6. Distance Validation
# ============================================================

def distance_validation_test(distance):
    """Test distance edge cases and invalid parameters."""

    X = np.array([
        [-2.0, 1.0],
        [3.0, -4.0],
        [5.0, 2.0],
    ])

    Y = np.array([
        [1.0, 3.0],
        [-5.0, 2.0],
    ])

    # --------------------------------------------------------
    # Unknown metric
    # --------------------------------------------------------

    try:
        distance(
            X,
            Y,
            metric="unknown",
        )
    except ValueError:
        pass
    else:
        _fail(
            "distance() must raise ValueError for an unknown metric."
        )

    # --------------------------------------------------------
    # Invalid Minkowski p
    # --------------------------------------------------------

    invalid_p_values = [
        0,
        -1,
        0.5,
        -2.5,
        np.nan,
        np.inf,
        -np.inf,
    ]

    for p in invalid_p_values:

        try:
            distance(
                X,
                Y,
                metric="minkowski",
                p=p,
            )
        except (ValueError, TypeError):
            pass
        else:
            _fail(
                f"Minkowski distance should reject invalid p={p}."
            )

    # --------------------------------------------------------
    # Non-negativity
    # --------------------------------------------------------

    for metric in [
        "euclidean",
        "manhattan",
        "chebyshev",
        "minkowski",
    ]:

        kwargs = {}

        if metric == "minkowski":
            kwargs["p"] = 2.5

        result = distance(
            X,
            Y,
            metric=metric,
            **kwargs,
        )

        if np.any(result < 0):
            _fail(
                f"{metric} distance cannot be negative."
            )

        if not np.all(np.isfinite(result)):
            _fail(
                f"{metric} distance contains NaN or infinity."
            )

    # --------------------------------------------------------
    # Single-point inputs
    # --------------------------------------------------------

    x = np.array([[0.0, 0.0]])
    y = np.array([[3.0, 4.0]])

    result = distance(
        x,
        y,
        metric="euclidean",
    )

    if result.shape != (1, 1):
        _fail(
            "distance() must support single-point inputs."
        )

    if not np.isclose(
        result[0, 0],
        5.0,
        atol=1e-8,
    ):
        _fail(
            "Single-point Euclidean distance is incorrect."
        )

    # --------------------------------------------------------
    # Different numbers of samples
    # --------------------------------------------------------

    X_many = np.array([
        [0.0, 0.0],
        [1.0, 1.0],
        [2.0, 2.0],
    ])

    Y_many = np.array([
        [0.0, 0.0],
        [10.0, 10.0],
        [20.0, 20.0],
        [30.0, 30.0],
    ])

    result = distance(
        X_many,
        Y_many,
        metric="euclidean",
    )

    if result.shape != (3, 4):
        _fail(
            "Pairwise distance returned the wrong shape "
            "for inputs with different numbers of samples."
        )

    # --------------------------------------------------------
    # Symmetry
    # --------------------------------------------------------

    for metric in [
        "euclidean",
        "manhattan",
        "chebyshev",
        "minkowski",
    ]:

        kwargs = {}

        if metric == "minkowski":
            kwargs["p"] = 3

        d_xy = distance(
            X,
            Y,
            metric=metric,
            **kwargs,
        )

        d_yx = distance(
            Y,
            X,
            metric=metric,
            **kwargs,
        )

        if not np.allclose(
            d_xy,
            d_yx.T,
            atol=1e-8,
        ):
            _fail(
                f"{metric} distance must be symmetric."
            )

    # --------------------------------------------------------
    # Zero diagonal
    # --------------------------------------------------------

    X_same = np.array([
        [1.5, -2.0, 7.0],
        [-4.0, 3.0, 0.5],
    ])

    for metric in [
        "euclidean",
        "manhattan",
        "chebyshev",
        "minkowski",
    ]:

        kwargs = {}

        if metric == "minkowski":
            kwargs["p"] = 2

        result = distance(
            X_same,
            X_same,
            metric=metric,
            **kwargs,
        )

        if not np.allclose(
            np.diag(result),
            0.0,
            atol=1e-8,
        ):
            _fail(
                f"{metric} distance from a point to itself "
                "must be zero."
            )

    _passed()


# ============================================================
# 7. Constructor
# ============================================================

def init_test(KNNClassifier):
    """Test default and custom KNN initialization."""

    # --------------------------------------------------------
    # Default parameters
    # --------------------------------------------------------

    try:
        knn = KNNClassifier()

        if knn.k != 5:
            _fail("Default k should be 5.")

        if knn.metric != "euclidean":
            _fail("Default metric should be 'euclidean'.")

        if knn.weights != "uniform":
            _fail("Default weights should be 'uniform'.")

        if knn.X_ is not None:
            _fail("X_ should initially be None.")

        if knn.y_ is not None:
            _fail("y_ should initially be None.")

    except AssertionError:
        raise
    except Exception as e:
        _fail(
            f"Unexpected error during initialization test: {e}"
        )

    # --------------------------------------------------------
    # Custom parameters
    # --------------------------------------------------------

    try:
        knn = KNNClassifier(
            k=7,
            metric="manhattan",
            weights="distance",
        )

        if knn.k != 7:
            _fail("Custom k was not stored correctly.")

        if knn.metric != "manhattan":
            _fail("Custom metric was not stored correctly.")

        if knn.weights != "distance":
            _fail("Custom weights were not stored correctly.")

    except AssertionError:
        raise
    except Exception as e:
        _fail(
            f"Unexpected error during custom initialization test: {e}"
        )

    _passed()


# ============================================================
# 8. Fit
# ============================================================

def fit_test(KNNClassifier):
    """Test that fit stores the training data and returns self."""

    X = np.array([
        [0.0, 0.0],
        [1.0, 1.0],
        [2.0, 2.0],
    ])

    y = np.array([0, 1, 1])

    try:
        knn = KNNClassifier(k=2)

        result = knn.fit(X, y)

        if result is not knn:
            _fail("fit() should return self.")

        if not np.array_equal(knn.X_, X):
            _fail("fit() did not correctly store X.")

        if not np.array_equal(knn.y_, y):
            _fail("fit() did not correctly store y.")

    except AssertionError:
        raise
    except Exception as e:
        _fail(
            f"Unexpected error during fit test: {e}"
        )

    _passed()


# ============================================================
# 9. Neighbor Search
# ============================================================

def neighbors_test(KNNClassifier):
    """Test nearest-neighbor indices and sorted distances."""

    X_train = np.array([
        [0.0, 0.0],
        [1.0, 1.0],
        [5.0, 5.0],
    ])

    y_train = np.array([0, 0, 1])

    X_test = np.array([
        [0.1, 0.1],
        [4.9, 4.9],
    ])

    try:
        knn = KNNClassifier(k=2)
        knn.fit(X_train, y_train)

        indices, distances = knn._get_neighbors(X_test)

        if indices.shape != (2, 2):
            _fail(
                f"Neighbor indices have shape {indices.shape}, "
                "expected (2, 2)."
            )

        if distances.shape != (2, 2):
            _fail(
                f"Neighbor distances have shape {distances.shape}, "
                "expected (2, 2)."
            )

        expected_first = np.array([0, 1])
        expected_second = np.array([2, 1])

        if not np.array_equal(
            indices[0],
            expected_first,
        ):
            _fail(
                f"Incorrect neighbors for first sample: "
                f"{indices[0]}, expected {expected_first}."
            )

        if not np.array_equal(
            indices[1],
            expected_second,
        ):
            _fail(
                f"Incorrect neighbors for second sample: "
                f"{indices[1]}, expected {expected_second}."
            )

        if not np.all(
            distances[:, :-1] <= distances[:, 1:]
        ):
            _fail(
                "Neighbor distances are not sorted."
            )

    except AssertionError:
        raise
    except Exception as e:
        _fail(
            f"Unexpected error during neighbor test: {e}"
        )

    _passed()


# ============================================================
# 10. Uniform Voting
# ============================================================

def uniform_vote_test(KNNClassifier):
    """Test majority voting with uniform weights."""

    X = np.array([
        [0.0],
        [1.0],
        [2.0],
    ])

    y = np.array([0, 1, 1])

    try:
        knn = KNNClassifier(
            k=3,
            weights="uniform",
        )

        knn.fit(X, y)

        neighbor_indices = np.array([0, 1, 2])

        prediction = knn._majority_vote(
            neighbor_indices
        )

        if prediction != 1:
            _fail(
                f"Uniform vote returned {prediction}, "
                "expected 1."
            )

    except AssertionError:
        raise
    except Exception as e:
        _fail(
            f"Unexpected error during uniform vote test: {e}"
        )

    _passed()


# ============================================================
# 11. Distance-Weighted Voting
# ============================================================

def distance_vote_test(KNNClassifier):
    """Test voting weighted by inverse distance."""

    X = np.array([
        [0.0],
        [10.0],
        [11.0],
    ])

    y = np.array([0, 1, 1])

    try:
        knn = KNNClassifier(
            k=3,
            weights="distance",
        )

        knn.fit(X, y)

        neighbor_indices = np.array([0, 1, 2])

        neighbor_distances = np.array([
            0.1,
            10.0,
            10.0,
        ])

        prediction = knn._majority_vote(
            neighbor_indices,
            neighbor_distances,
        )

        if prediction != 0:
            _fail(
                f"Distance vote returned {prediction}, "
                "expected 0."
            )

    except AssertionError:
        raise
    except Exception as e:
        _fail(
            f"Unexpected error during distance vote test: {e}"
        )

    _passed()


# ============================================================
# 12. Prediction
# ============================================================

def predict_test(KNNClassifier):
    """Test KNN predictions on multiple query samples."""

    X_train = np.array([
        [0.0, 0.0],
        [0.0, 1.0],
        [5.0, 5.0],
        [5.0, 6.0],
    ])

    y_train = np.array([
        0,
        0,
        1,
        1,
    ])

    X_test = np.array([
        [0.1, 0.1],
        [0.2, 0.2],
        [5.1, 5.1],
        [5.2, 5.2],
    ])

    expected = np.array([
        0,
        0,
        1,
        1,
    ])

    try:
        knn = KNNClassifier(k=3)
        knn.fit(X_train, y_train)

        predictions = knn.predict(X_test)

        if predictions.shape != (4,):
            _fail(
                f"predict() returned shape {predictions.shape}, "
                "expected (4,)."
            )

        if not np.array_equal(
            predictions,
            expected,
        ):
            _fail(
                f"predict() returned {predictions}, "
                f"expected {expected}."
            )

    except AssertionError:
        raise
    except Exception as e:
        _fail(
            f"Unexpected error during prediction test: {e}"
        )

    _passed()


# ============================================================
# 13. Parameter Validation
# ============================================================

def parameter_validation_test(KNNClassifier):
    """Test validation of constructor and fitting parameters."""

    # --------------------------------------------------------
    # Invalid k type
    # --------------------------------------------------------

    for k in [2.5, "5", True]:

        try:
            KNNClassifier(k=k)

        except TypeError:
            pass

        except Exception as e:
            _fail(
                f"k={k!r} raised the wrong exception: "
                f"{type(e).__name__}."
            )

        else:
            _fail(
                f"k={k!r} should raise TypeError."
            )

    # --------------------------------------------------------
    # Invalid k value
    # --------------------------------------------------------

    for k in [0, -1, -5]:

        try:
            KNNClassifier(k=k)

        except ValueError:
            pass

        except Exception as e:
            _fail(
                f"k={k} raised the wrong exception: "
                f"{type(e).__name__}."
            )

        else:
            _fail(
                f"k={k} should raise ValueError."
            )

    # --------------------------------------------------------
    # Invalid metric
    # --------------------------------------------------------

    try:
        KNNClassifier(metric="invalid")

    except ValueError:
        pass

    except Exception as e:
        _fail(
            "Invalid metric raised "
            f"{type(e).__name__} instead of ValueError."
        )

    else:
        _fail(
            "Invalid metric should raise ValueError."
        )

    # --------------------------------------------------------
    # Invalid weights
    # --------------------------------------------------------

    try:
        KNNClassifier(weights="invalid")

    except ValueError:
        pass

    except Exception as e:
        _fail(
            "Invalid weights raised "
            f"{type(e).__name__} instead of ValueError."
        )

    else:
        _fail(
            "Invalid weights should raise ValueError."
        )

    # --------------------------------------------------------
    # Invalid Minkowski p type
    # --------------------------------------------------------

    for p in ["3", True]:

        try:
            KNNClassifier(
                metric="minkowski",
                p=p,
            )

        except TypeError:
            pass

        except Exception as e:
            _fail(
                f"p={p!r} raised the wrong exception: "
                f"{type(e).__name__}."
            )

        else:
            _fail(
                f"p={p!r} should raise TypeError."
            )

    # --------------------------------------------------------
    # Invalid Minkowski p value
    # --------------------------------------------------------

    for p in [0, -1]:

        try:
            KNNClassifier(
                metric="minkowski",
                p=p,
            )

        except ValueError:
            pass

        except Exception as e:
            _fail(
                f"p={p} raised the wrong exception: "
                f"{type(e).__name__}."
            )

        else:
            _fail(
                f"p={p} should raise ValueError."
            )

    # --------------------------------------------------------
    # k greater than number of training samples
    # --------------------------------------------------------

    X = np.array([
        [0.0],
        [1.0],
        [2.0],
    ])

    y = np.array([0, 1, 1])

    knn = KNNClassifier(k=4)

    try:
        knn.fit(X, y)

    except ValueError:
        pass

    except Exception as e:
        _fail(
            "k > n_samples raised "
            f"{type(e).__name__} instead of ValueError."
        )

    else:
        _fail(
            "fit() should raise ValueError when "
            "k > number of training samples."
        )

    # --------------------------------------------------------
    # Mismatched X/y
    # --------------------------------------------------------

    X = np.array([
        [0.0, 0.0],
        [1.0, 1.0],
    ])

    y = np.array([0])

    knn = KNNClassifier(k=1)

    try:
        knn.fit(X, y)

    except ValueError:
        pass

    except Exception as e:
        _fail(
            "Mismatched X/y raised "
            f"{type(e).__name__} instead of ValueError."
        )

    else:
        _fail(
            "fit() should raise ValueError when "
            "X and y have different numbers of samples."
        )

    # --------------------------------------------------------
    # X must be 2D
    # --------------------------------------------------------

    X = np.array([
        0.0,
        1.0,
        2.0,
    ])

    y = np.array([0, 1, 1])

    knn = KNNClassifier(k=1)

    try:
        knn.fit(X, y)

    except ValueError:
        pass

    except Exception as e:
        _fail(
            "1D X raised "
            f"{type(e).__name__} instead of ValueError."
        )

    else:
        _fail(
            "fit() should raise ValueError when X is not 2D."
        )

    _passed()