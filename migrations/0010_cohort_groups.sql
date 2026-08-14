-- Rewrite stored cohort sampling from purchased_n / all_n / any_n into a_n / b_n
-- groups. Python owns the mapping so it is not duplicated in SQL; membership is
-- not resampled, and `selection` stays the historical sentence.
SELECT 1;
