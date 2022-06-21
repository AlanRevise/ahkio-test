from odoo import fields, models


class Partner(models.Model):
    _inherit = "res.partner"

    ahkio_organisation_unit_number = fields.Char(string='Organisation unit number (OVT)')

    ahkio_e_invoice_address = fields.Char(string='E-invoice address')
    ahkio_e_invoice_intermediator = fields.Char(string='E-invoice intermediator')
