-- Golden schema dump from prod snapshot
-- source: smashbox-prod-snapshot.db

CREATE TABLE ad_credits (
	id INTEGER NOT NULL, 
	year INTEGER NOT NULL, 
	month INTEGER NOT NULL, 
	amount NUMERIC(14, 2) NOT NULL, 
	note VARCHAR(512), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, shop_id INTEGER REFERENCES shops(id), applied_date DATE, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_ad_credits_year_month UNIQUE (year, month)
);

CREATE TABLE ad_spend (
	id INTEGER NOT NULL, 
	import_batch_id INTEGER NOT NULL, 
	spend_date DATETIME NOT NULL, 
	campaign_id VARCHAR(64) NOT NULL, 
	campaign_name VARCHAR(512), 
	cash_cost NUMERIC(14, 2) NOT NULL, 
	credit_cost NUMERIC(14, 2) NOT NULL, 
	ad_credit_cost NUMERIC(14, 2) NOT NULL, 
	amount NUMERIC(14, 2) NOT NULL, 
	currency VARCHAR(8) NOT NULL, 
	campaign_type VARCHAR(64), shop_id INTEGER REFERENCES shops(id), 
	PRIMARY KEY (id), 
	CONSTRAINT uq_ad_spend_date_campaign UNIQUE (spend_date, campaign_id), 
	FOREIGN KEY(import_batch_id) REFERENCES import_batches (id)
);

CREATE TABLE adjustments (
	id INTEGER NOT NULL, 
	import_batch_id INTEGER NOT NULL, 
	adjustment_id VARCHAR(64) NOT NULL, 
	adjustment_type VARCHAR(128) NOT NULL, 
	reason VARCHAR(256), 
	amount NUMERIC(14, 2) NOT NULL, 
	create_time DATETIME, 
	settlement_time DATETIME, 
	linked_statement_id VARCHAR(64), 
	linked_payout_id VARCHAR(64), shop_id INTEGER REFERENCES shops(id), 
	PRIMARY KEY (id), 
	CONSTRAINT uq_adjustment_natural_key UNIQUE (adjustment_id, adjustment_type, create_time), 
	FOREIGN KEY(import_batch_id) REFERENCES import_batches (id)
);

CREATE TABLE bundle_components (
	id INTEGER NOT NULL, 
	bundle_id INTEGER NOT NULL, 
	component_sku VARCHAR(128) NOT NULL, 
	component_name VARCHAR(512), 
	quantity INTEGER NOT NULL, 
	msrp NUMERIC(12, 2) NOT NULL, 
	unit_cogs NUMERIC(12, 4) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(bundle_id) REFERENCES bundles (id)
);

CREATE TABLE bundles (
	id INTEGER NOT NULL, 
	bundle_sku VARCHAR(128), 
	tiktok_sku_id VARCHAR(64), 
	name VARCHAR(512) NOT NULL, 
	variation VARCHAR(256), 
	brand VARCHAR(64) NOT NULL, 
	is_active VARCHAR(16) NOT NULL, 
	msrp NUMERIC(12, 2) NOT NULL, 
	selling_price NUMERIC(12, 2) NOT NULL, shop_id INTEGER REFERENCES shops(id), 
	PRIMARY KEY (id)
);

CREATE TABLE creators (
	id INTEGER NOT NULL, 
	shop_id INTEGER, 
	handle VARCHAR(256) NOT NULL, 
	name VARCHAR(512), 
	platform VARCHAR(64) NOT NULL, 
	brand VARCHAR(64) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_creator_per_shop_platform UNIQUE (shop_id, handle, platform), 
	FOREIGN KEY(shop_id) REFERENCES shops (id)
);

CREATE TABLE gmv_max_campaign_metrics (
	id INTEGER NOT NULL, 
	shop_id INTEGER, 
	year INTEGER NOT NULL, 
	month INTEGER NOT NULL, 
	gross_revenue NUMERIC(14, 2) NOT NULL, 
	sku_orders INTEGER NOT NULL, 
	note VARCHAR(512), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_gmv_max_campaign_metrics_year_month UNIQUE (year, month), 
	FOREIGN KEY(shop_id) REFERENCES shops (id)
);

CREATE TABLE gmv_max_daily_metrics (
	id INTEGER NOT NULL, 
	import_batch_id INTEGER NOT NULL, 
	shop_id INTEGER, 
	metric_date DATE NOT NULL, 
	cost NUMERIC(14, 2) NOT NULL, 
	sku_orders INTEGER NOT NULL, 
	gross_revenue NUMERIC(14, 2) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_gmv_max_daily_date UNIQUE (metric_date), 
	FOREIGN KEY(import_batch_id) REFERENCES import_batches (id), 
	FOREIGN KEY(shop_id) REFERENCES shops (id)
);

CREATE TABLE gmv_max_reimbursements (
	id INTEGER NOT NULL, 
	shop_id INTEGER, 
	year INTEGER NOT NULL, 
	month INTEGER NOT NULL, 
	amount NUMERIC(12, 2) NOT NULL, 
	note VARCHAR(512), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_gmv_max_reimbursements_year_month UNIQUE (year, month), 
	FOREIGN KEY(shop_id) REFERENCES shops (id)
);

CREATE TABLE import_batches (
	id INTEGER NOT NULL, 
	kind VARCHAR(18) NOT NULL, 
	status VARCHAR(10) NOT NULL, 
	original_filename VARCHAR(512) NOT NULL, 
	stored_path VARCHAR(1024) NOT NULL, 
	uploaded_at DATETIME NOT NULL, 
	completed_at DATETIME, 
	rows_imported INTEGER NOT NULL, 
	rows_skipped INTEGER NOT NULL, 
	error_message TEXT, shop_id INTEGER REFERENCES shops(id), 
	PRIMARY KEY (id)
);

CREATE TABLE inventory_snapshots (
	id INTEGER NOT NULL, 
	shop_id INTEGER, 
	import_batch_id INTEGER NOT NULL, 
	sku VARCHAR(128) NOT NULL, 
	on_hand INTEGER NOT NULL, 
	captured_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_inventory_sku_captured_at UNIQUE (sku, captured_at), 
	FOREIGN KEY(shop_id) REFERENCES shops (id), 
	FOREIGN KEY(import_batch_id) REFERENCES import_batches (id)
);

CREATE TABLE invoices (
	id INTEGER NOT NULL, 
	number VARCHAR(32) NOT NULL, 
	issue_date DATE NOT NULL, 
	bill_to_block TEXT NOT NULL, 
	description_headline VARCHAR(256) NOT NULL, 
	description_subtitle TEXT, 
	period_label VARCHAR(128), 
	amount NUMERIC(12, 2) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	brand_code VARCHAR(32) NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE order_lines (
	id INTEGER NOT NULL, 
	order_id INTEGER NOT NULL, 
	sku VARCHAR(128) NOT NULL, 
	quantity INTEGER NOT NULL, 
	unit_price NUMERIC(12, 2) NOT NULL, 
	gross_sales NUMERIC(14, 2) NOT NULL, 
	platform_discount NUMERIC(14, 2) NOT NULL, 
	post_tiktok_price NUMERIC(14, 2) NOT NULL, 
	seller_funded_discount NUMERIC(14, 2) NOT NULL, 
	seller_funded_outlandish NUMERIC(14, 2) NOT NULL, 
	seller_funded_smashbox NUMERIC(14, 2) NOT NULL, 
	discount_policy_violation BOOLEAN NOT NULL, 
	unit_cogs_snapshot NUMERIC(12, 4) NOT NULL, policy_violation_acknowledged BOOLEAN NOT NULL DEFAULT 0, policy_violation_acknowledged_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(order_id) REFERENCES orders (id)
);

CREATE TABLE orders (
	id INTEGER NOT NULL, 
	import_batch_id INTEGER NOT NULL, 
	tiktok_order_id VARCHAR(64) NOT NULL, 
	placed_at DATETIME NOT NULL, 
	order_type VARCHAR(11) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	brand VARCHAR(64) NOT NULL, 
	gross_sales NUMERIC(14, 2) NOT NULL, 
	platform_discount_total NUMERIC(14, 2) NOT NULL, 
	refunds NUMERIC(14, 2) NOT NULL, 
	shipping_revenue NUMERIC(14, 2) NOT NULL, 
	shipping_cost NUMERIC(14, 2) NOT NULL, 
	tiktok_fees NUMERIC(14, 2) NOT NULL, 
	tiktok_referral_fee NUMERIC(14, 2) NOT NULL, 
	tiktok_transaction_fee NUMERIC(14, 2) NOT NULL, 
	tiktok_refund_admin_fee NUMERIC(14, 2) NOT NULL, 
	tiktok_sales_tax_on_referral NUMERIC(14, 2) NOT NULL, 
	tiktok_smart_promo_fee NUMERIC(14, 2) NOT NULL, 
	tiktok_campaign_fees NUMERIC(14, 2) NOT NULL, 
	tiktok_partner_commission NUMERIC(14, 2) NOT NULL, 
	tiktok_managed_service NUMERIC(14, 2) NOT NULL, 
	affiliate_commission NUMERIC(14, 2) NOT NULL, 
	shop_ads_cost NUMERIC(14, 2) NOT NULL, 
	seller_funded_discount_total NUMERIC(14, 2) NOT NULL, 
	seller_funded_outlandish NUMERIC(14, 2) NOT NULL, 
	seller_funded_smashbox NUMERIC(14, 2) NOT NULL, 
	discount_policy_violation BOOLEAN NOT NULL, shop_id INTEGER REFERENCES shops(id), payment_platform_discount NUMERIC(14,2) NOT NULL DEFAULT 0, 
	PRIMARY KEY (id), 
	FOREIGN KEY(import_batch_id) REFERENCES import_batches (id)
);

CREATE TABLE payouts (
	id INTEGER NOT NULL, 
	import_batch_id INTEGER NOT NULL, 
	payout_id VARCHAR(64) NOT NULL, 
	paid_at DATETIME NOT NULL, 
	period_start DATETIME, 
	period_end DATETIME, 
	gross_amount NUMERIC(14, 2) NOT NULL, 
	fees NUMERIC(14, 2) NOT NULL, 
	net_amount NUMERIC(14, 2) NOT NULL, 
	currency VARCHAR(8) NOT NULL, shop_id INTEGER REFERENCES shops(id), 
	PRIMARY KEY (id), 
	FOREIGN KEY(import_batch_id) REFERENCES import_batches (id)
);

CREATE TABLE purchase_invoice_credits (
	id INTEGER NOT NULL, 
	purchase_invoice_id INTEGER NOT NULL, 
	credit_date DATE NOT NULL, 
	amount NUMERIC(12, 2) NOT NULL, 
	reason TEXT, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(purchase_invoice_id) REFERENCES purchase_invoices (id)
);

CREATE TABLE purchase_invoice_payments (
	id INTEGER NOT NULL, 
	purchase_invoice_id INTEGER NOT NULL, 
	payment_date DATE NOT NULL, 
	amount NUMERIC(12, 2) NOT NULL, 
	reference TEXT, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(purchase_invoice_id) REFERENCES purchase_invoices (id)
);

CREATE TABLE purchase_invoices (
	id INTEGER NOT NULL, 
	shop_id INTEGER, 
	number VARCHAR(64) NOT NULL, 
	invoice_date DATE NOT NULL, 
	amount NUMERIC(12, 2) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	note TEXT, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, due_date DATE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(shop_id) REFERENCES shops (id)
);

CREATE TABLE sample_allowances (
	id INTEGER NOT NULL, 
	brand VARCHAR(64) NOT NULL, 
	year INTEGER NOT NULL, 
	month INTEGER NOT NULL, 
	allowance_units INTEGER NOT NULL, 
	notes VARCHAR(512), 
	PRIMARY KEY (id), 
	CONSTRAINT uq_allowance_brand_period UNIQUE (brand, year, month)
);

CREATE TABLE sample_inventory_movements (
	id INTEGER NOT NULL, 
	shop_id INTEGER, 
	import_batch_id INTEGER, 
	brand VARCHAR(64) NOT NULL, 
	sku VARCHAR(128) NOT NULL, 
	movement_type VARCHAR(3) NOT NULL, 
	quantity INTEGER NOT NULL, 
	moved_at DATETIME NOT NULL, 
	unit_cost NUMERIC(12, 4), 
	sample_id INTEGER, 
	note VARCHAR(1024), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(shop_id) REFERENCES shops (id), 
	FOREIGN KEY(import_batch_id) REFERENCES import_batches (id), 
	FOREIGN KEY(sample_id) REFERENCES samples (id)
);

CREATE TABLE samples (
	id INTEGER NOT NULL, 
	import_batch_id INTEGER NOT NULL, 
	shipped_at DATETIME NOT NULL, 
	sku VARCHAR(128) NOT NULL, 
	quantity INTEGER NOT NULL, 
	creator_handle VARCHAR(256), 
	is_paid_oversample BOOLEAN NOT NULL, 
	note VARCHAR(1024), shop_id INTEGER REFERENCES shops(id), shipping_cost NUMERIC(12,2), creator_id INTEGER REFERENCES creators(id), 
	PRIMARY KEY (id), 
	FOREIGN KEY(import_batch_id) REFERENCES import_batches (id)
);

CREATE TABLE settlements (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	import_batch_id INTEGER NOT NULL, 
	tiktok_order_id VARCHAR(64) NOT NULL, 
	linked_statement_id VARCHAR(64), 
	linked_payout_id VARCHAR(64), 
	paid_date DATETIME, 
	settled_date DATETIME, 
	order_status VARCHAR(64), 
	sample_order_type VARCHAR(128), 
	order_income NUMERIC(14, 2) NOT NULL, 
	order_cost NUMERIC(14, 2) NOT NULL, 
	net_order_margin NUMERIC(14, 2) NOT NULL, 
	gross_sales NUMERIC(14, 2) NOT NULL, 
	gross_sales_refund NUMERIC(14, 2) NOT NULL, 
	seller_discount NUMERIC(14, 2) NOT NULL, 
	seller_discount_refund NUMERIC(14, 2) NOT NULL, 
	tiktok_fees NUMERIC(14, 2) NOT NULL, 
	tiktok_referral_fee NUMERIC(14, 2) NOT NULL, 
	tiktok_transaction_fee NUMERIC(14, 2) NOT NULL, 
	tiktok_refund_admin_fee NUMERIC(14, 2) NOT NULL, 
	tiktok_sales_tax_on_referral NUMERIC(14, 2) NOT NULL, 
	tiktok_smart_promo_fee NUMERIC(14, 2) NOT NULL, 
	tiktok_campaign_fees NUMERIC(14, 2) NOT NULL, 
	tiktok_partner_commission NUMERIC(14, 2) NOT NULL, 
	tiktok_managed_service NUMERIC(14, 2) NOT NULL, 
	affiliate_commission NUMERIC(14, 2) NOT NULL, 
	shop_ads_cost NUMERIC(14, 2) NOT NULL, 
	shipping_cost NUMERIC(14, 2) NOT NULL, 
	raw_payload JSON, shop_id INTEGER REFERENCES shops(id), 
	CONSTRAINT uq_settlement_order_statement UNIQUE (tiktok_order_id, linked_statement_id), 
	FOREIGN KEY(import_batch_id) REFERENCES import_batches (id)
);

CREATE TABLE shops (
	id INTEGER NOT NULL, 
	slug VARCHAR(64) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	timezone VARCHAR(64) NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE sku_aliases (
	id INTEGER NOT NULL, 
	shop_id INTEGER, 
	alias_sku VARCHAR(128) NOT NULL, 
	canonical_sku VARCHAR(128) NOT NULL, 
	notes VARCHAR(512), 
	created_at DATETIME NOT NULL, 
	created_by_user_id INTEGER, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_sku_alias_per_shop UNIQUE (shop_id, alias_sku), 
	FOREIGN KEY(shop_id) REFERENCES shops (id), 
	FOREIGN KEY(created_by_user_id) REFERENCES users (id)
);

CREATE TABLE skus (
	id INTEGER NOT NULL, 
	sku VARCHAR(128) NOT NULL, 
	tiktok_alt_sku VARCHAR(128), 
	tiktok_sku_id VARCHAR(64), 
	name VARCHAR(512) NOT NULL, 
	brand VARCHAR(64) NOT NULL, 
	category VARCHAR(256), 
	item_type VARCHAR(64), 
	msrp NUMERIC(12, 2) NOT NULL, 
	unit_cogs NUMERIC(12, 4) NOT NULL, 
	is_active BOOLEAN NOT NULL, shop_id INTEGER REFERENCES shops(id), lead_time_days INTEGER, moq INTEGER, case_pack INTEGER, safety_stock_pct NUMERIC(5,2), is_reorderable BOOLEAN NOT NULL DEFAULT 1, service_level NUMERIC(4,3), 
	PRIMARY KEY (id)
);

CREATE TABLE sqlite_sequence(name,seq);

CREATE TABLE tiktok_daily_metrics (
	id INTEGER NOT NULL, 
	import_batch_id INTEGER NOT NULL, 
	metric_date DATE NOT NULL, 
	gmv NUMERIC(14, 2) NOT NULL, 
	orders INTEGER NOT NULL, 
	customers INTEGER NOT NULL, 
	items_sold INTEGER NOT NULL, 
	items_canceled_returned INTEGER NOT NULL, 
	items_refunded INTEGER NOT NULL, 
	aov NUMERIC(14, 2) NOT NULL, 
	gmv_with_tax NUMERIC(14, 2) NOT NULL, 
	tax NUMERIC(14, 2) NOT NULL, 
	shipping_fees NUMERIC(14, 2) NOT NULL, shop_id INTEGER REFERENCES shops(id), 
	PRIMARY KEY (id), 
	CONSTRAINT uq_tt_daily_date UNIQUE (metric_date), 
	FOREIGN KEY(import_batch_id) REFERENCES import_batches (id)
);

CREATE TABLE users (
	id INTEGER NOT NULL, 
	email VARCHAR(255) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	password_hash VARCHAR(255) NOT NULL, 
	role VARCHAR(6) NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	last_login_at DATETIME, shop_id INTEGER REFERENCES shops(id), is_super_admin BOOLEAN NOT NULL DEFAULT 0, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_ad_credits_month ON ad_credits (month);

CREATE INDEX ix_ad_credits_year ON ad_credits (year);

CREATE INDEX ix_ad_spend_campaign_id ON ad_spend (campaign_id);

CREATE INDEX ix_ad_spend_import_batch_id ON ad_spend (import_batch_id);

CREATE INDEX ix_ad_spend_spend_date ON ad_spend (spend_date);

CREATE INDEX ix_adjustments_adjustment_id ON adjustments (adjustment_id);

CREATE INDEX ix_adjustments_adjustment_type ON adjustments (adjustment_type);

CREATE INDEX ix_adjustments_create_time ON adjustments (create_time);

CREATE INDEX ix_adjustments_import_batch_id ON adjustments (import_batch_id);

CREATE INDEX ix_adjustments_linked_payout_id ON adjustments (linked_payout_id);

CREATE INDEX ix_adjustments_linked_statement_id ON adjustments (linked_statement_id);

CREATE INDEX ix_adjustments_settlement_time ON adjustments (settlement_time);

CREATE INDEX ix_bundle_components_bundle_id ON bundle_components (bundle_id);

CREATE INDEX ix_bundle_components_component_sku ON bundle_components (component_sku);

CREATE INDEX ix_bundles_brand ON bundles (brand);

CREATE INDEX ix_bundles_bundle_sku ON bundles (bundle_sku);

CREATE INDEX ix_bundles_tiktok_sku_id ON bundles (tiktok_sku_id);

CREATE INDEX ix_creators_brand ON creators (brand);

CREATE INDEX ix_creators_handle ON creators (handle);

CREATE INDEX ix_creators_shop_id ON creators (shop_id);

CREATE INDEX ix_gmv_max_campaign_metrics_month ON gmv_max_campaign_metrics (month);

CREATE INDEX ix_gmv_max_campaign_metrics_shop_id ON gmv_max_campaign_metrics (shop_id);

CREATE INDEX ix_gmv_max_campaign_metrics_year ON gmv_max_campaign_metrics (year);

CREATE INDEX ix_gmv_max_daily_metrics_import_batch_id ON gmv_max_daily_metrics (import_batch_id);

CREATE INDEX ix_gmv_max_daily_metrics_metric_date ON gmv_max_daily_metrics (metric_date);

CREATE INDEX ix_gmv_max_daily_metrics_shop_id ON gmv_max_daily_metrics (shop_id);

CREATE INDEX ix_gmv_max_reimbursements_month ON gmv_max_reimbursements (month);

CREATE INDEX ix_gmv_max_reimbursements_shop_id ON gmv_max_reimbursements (shop_id);

CREATE INDEX ix_gmv_max_reimbursements_year ON gmv_max_reimbursements (year);

CREATE INDEX ix_import_batches_kind ON import_batches (kind);

CREATE INDEX ix_import_batches_status ON import_batches (status);

CREATE INDEX ix_inventory_snapshots_captured_at ON inventory_snapshots (captured_at);

CREATE INDEX ix_inventory_snapshots_import_batch_id ON inventory_snapshots (import_batch_id);

CREATE INDEX ix_inventory_snapshots_shop_id ON inventory_snapshots (shop_id);

CREATE INDEX ix_inventory_snapshots_sku ON inventory_snapshots (sku);

CREATE INDEX ix_invoices_brand_code ON invoices (brand_code);

CREATE UNIQUE INDEX ix_invoices_number ON invoices (number);

CREATE INDEX ix_order_lines_order_id ON order_lines (order_id);

CREATE INDEX ix_order_lines_sku ON order_lines (sku);

CREATE INDEX ix_orders_brand ON orders (brand);

CREATE INDEX ix_orders_discount_policy_violation ON orders (discount_policy_violation);

CREATE INDEX ix_orders_import_batch_id ON orders (import_batch_id);

CREATE INDEX ix_orders_order_type ON orders (order_type);

CREATE INDEX ix_orders_placed_at ON orders (placed_at);

CREATE INDEX ix_orders_status ON orders (status);

CREATE UNIQUE INDEX ix_orders_tiktok_order_id ON orders (tiktok_order_id);

CREATE INDEX ix_payouts_import_batch_id ON payouts (import_batch_id);

CREATE INDEX ix_payouts_paid_at ON payouts (paid_at);

CREATE UNIQUE INDEX ix_payouts_payout_id ON payouts (payout_id);

CREATE INDEX ix_purchase_invoice_credits_purchase_invoice_id ON purchase_invoice_credits (purchase_invoice_id);

CREATE INDEX ix_purchase_invoice_payments_purchase_invoice_id ON purchase_invoice_payments (purchase_invoice_id);

CREATE UNIQUE INDEX ix_purchase_invoices_number ON purchase_invoices (number);

CREATE INDEX ix_purchase_invoices_shop_id ON purchase_invoices (shop_id);

CREATE INDEX ix_sample_allowances_brand ON sample_allowances (brand);

CREATE INDEX ix_sample_allowances_year ON sample_allowances (year);

CREATE INDEX ix_sample_inventory_movements_brand ON sample_inventory_movements (brand);

CREATE INDEX ix_sample_inventory_movements_import_batch_id ON sample_inventory_movements (import_batch_id);

CREATE INDEX ix_sample_inventory_movements_moved_at ON sample_inventory_movements (moved_at);

CREATE INDEX ix_sample_inventory_movements_sample_id ON sample_inventory_movements (sample_id);

CREATE INDEX ix_sample_inventory_movements_shop_id ON sample_inventory_movements (shop_id);

CREATE INDEX ix_sample_inventory_movements_sku ON sample_inventory_movements (sku);

CREATE INDEX ix_samples_import_batch_id ON samples (import_batch_id);

CREATE INDEX ix_samples_is_paid_oversample ON samples (is_paid_oversample);

CREATE INDEX ix_samples_shipped_at ON samples (shipped_at);

CREATE INDEX ix_samples_sku ON samples (sku);

CREATE INDEX ix_settlements_import_batch_id ON settlements (import_batch_id);

CREATE INDEX ix_settlements_linked_payout_id ON settlements (linked_payout_id);

CREATE INDEX ix_settlements_linked_statement_id ON settlements (linked_statement_id);

CREATE INDEX ix_settlements_paid_date ON settlements (paid_date);

CREATE INDEX ix_settlements_sample_order_type ON settlements (sample_order_type);

CREATE INDEX ix_settlements_settled_date ON settlements (settled_date);

CREATE INDEX ix_settlements_tiktok_order_id ON settlements (tiktok_order_id);

CREATE INDEX ix_shops_is_active ON shops (is_active);

CREATE UNIQUE INDEX ix_shops_slug ON shops (slug);

CREATE INDEX ix_sku_aliases_alias_sku ON sku_aliases (alias_sku);

CREATE INDEX ix_sku_aliases_canonical_sku ON sku_aliases (canonical_sku);

CREATE INDEX ix_sku_aliases_shop_id ON sku_aliases (shop_id);

CREATE INDEX ix_skus_brand ON skus (brand);

CREATE INDEX ix_skus_sku ON skus (sku);

CREATE INDEX ix_skus_tiktok_alt_sku ON skus (tiktok_alt_sku);

CREATE UNIQUE INDEX ix_skus_tiktok_sku_id ON skus (tiktok_sku_id);

CREATE INDEX ix_tiktok_daily_metrics_import_batch_id ON tiktok_daily_metrics (import_batch_id);

CREATE INDEX ix_tiktok_daily_metrics_metric_date ON tiktok_daily_metrics (metric_date);

CREATE UNIQUE INDEX ix_users_email ON users (email);

CREATE INDEX ix_users_is_active ON users (is_active);

CREATE INDEX ix_users_role ON users (role);

