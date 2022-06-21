from odoo import models, fields


class Partner(models.Model):
    _inherit = "res.partner"
    
    ahkio_company_registry = fields.Char(string="Company registry")
