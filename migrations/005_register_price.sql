-- 005: ціна каси за кг (для розрахунку ваги з чеків Octobox: вага = сума до знижки / ціна за кг)
ALTER TABLE products ADD COLUMN register_price TEXT;
