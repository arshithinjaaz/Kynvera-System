"""
Database Models for Kynvera
SQLAlchemy ORM models for PostgreSQL/SQLite
"""
import json
from datetime import date, datetime, timezone
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from sqlalchemy import JSON

from common.datetime_utils import naive_utc_isoformat_z

db = SQLAlchemy()
bcrypt = Bcrypt()


def _utcnow():
    """Naive UTC datetime for SQLAlchemy column defaults (timezone-unaware columns)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(db.Model):
    """User accounts with role-based access"""
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    # Plaintext copy for admin Manage profile (set whenever password is assigned/reset)
    admin_visible_password = db.Column(db.String(255), nullable=True)
    full_name = db.Column(db.String(120))
    role = db.Column(db.String(20), default='user')  # 'admin', 'user'
    designation = db.Column(db.String(30), default=None)  # 'supervisor', 'operations_manager', 'business_development', 'procurement', 'general_manager'
    is_active = db.Column(db.Boolean, default=True)
    password_changed = db.Column(db.Boolean, default=False)  # Track if password was changed from default
    default_signature = db.Column(db.Text, default=None)  # Base64 data URL for default signature
    default_comment = db.Column(db.Text, default=None)  # Default comment for approvals
    # Module access permissions (admin has access to all by default)
    access_hvac = db.Column(db.Boolean, default=False)  # HVAC&MEP form access
    access_civil = db.Column(db.Boolean, default=False)  # Civil works form access
    access_cleaning = db.Column(db.Boolean, default=False)  # Cleaning form access
    access_hr = db.Column(db.Boolean, default=False)  # HR module access (forms)
    access_hiring = db.Column(db.Boolean, default=False)  # HR submodule: hiring docs, leave tracker, manpower
    access_procurement_module = db.Column(db.Boolean, default=False)  # Procurement module access
    access_business_development = db.Column(db.Boolean, default=False)  # BD pipeline + email + inspection BD reviewer (when no conflicting designation)
    access_sales_manager = db.Column(db.Boolean, default=False)  # BD: view all salespeople's pipelines
    access_quotations = db.Column(db.Boolean, default=False)  # BD: create/edit/submit quotations
    access_report_generation = db.Column(db.Boolean, default=False)  # MMR / Report Generation hub
    access_submitted_forms = db.Column(db.Boolean, default=False)  # "My submitted forms" workflow hub
    access_ticketing = db.Column(db.Boolean, default=False)  # Ticketing / Work Order module
    access_qhsi = db.Column(db.Boolean, default=False)  # QHSI — quality, hospitality, safety & inspections
    access_files = db.Column(db.Boolean, default=False)  # Files module (Finder + Drive sync)
    # Pre-designated department representative allowed to appear in the ticket "Reported By" list.
    is_ticket_reporter = db.Column(db.Boolean, default=False)
    # MFA (TOTP)
    mfa_enabled = db.Column(db.Boolean, default=False)
    mfa_secret = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    last_login = db.Column(db.DateTime)
    # First day with the company (for tenure on dashboard); editable in Profile / admin Manage profile
    employment_start_date = db.Column(db.Date, nullable=True)
    # HR / org fields (distinct from workflow `designation`; set by admin, visible on profile)
    job_designation = db.Column(db.String(160), nullable=True)
    annual_leave_days = db.Column(db.Integer, nullable=True)
    other_leave_days = db.Column(db.Integer, nullable=True)
    reporting_manager_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    # Operations manager assigned to this user by admin (used for technician HR routing).
    operations_manager_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    phone = db.Column(db.String(40), nullable=True)
    assigned_project = db.Column(db.String(200), nullable=True)

    # Relationships
    submissions = db.relationship('Submission', foreign_keys='Submission.user_id', backref='user', lazy='dynamic')
    supervised_submissions = db.relationship('Submission', foreign_keys='Submission.supervisor_id', backref='supervisor', lazy='dynamic')
    ops_manager_submissions = db.relationship('Submission', foreign_keys='Submission.operations_manager_id', backref='operations_manager', lazy='dynamic')
    business_dev_submissions = db.relationship('Submission', foreign_keys='Submission.business_dev_id', backref='business_dev', lazy='dynamic')
    procurement_submissions = db.relationship('Submission', foreign_keys='Submission.procurement_id', backref='procurement_user', lazy='dynamic')
    general_manager_submissions = db.relationship('Submission', foreign_keys='Submission.general_manager_id', backref='general_manager', lazy='dynamic')
    # Legacy
    managed_submissions = db.relationship('Submission', foreign_keys='Submission.manager_id', backref='manager', lazy='dynamic')
    audit_logs = db.relationship('AuditLog', backref='user', lazy='dynamic')
    sessions = db.relationship('Session', backref='user', lazy='dynamic', cascade='all, delete-orphan')
    reporting_manager = db.relationship(
        'User',
        foreign_keys=[reporting_manager_id],
        remote_side=[id],
    )
    operations_manager = db.relationship(
        'User',
        foreign_keys=[operations_manager_id],
        remote_side=[id],
    )

    def set_password(self, password):
        """Hash and set password. Never persist plaintext."""
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')
        if hasattr(self, 'admin_visible_password'):
            self.admin_visible_password = None
    
    def check_password(self, password):
        """Verify password against hash"""
        return bcrypt.check_password_hash(self.password_hash, password)
    
    def has_module_access(self, module):
        """Check if user has access to a specific module"""
        if self.role == 'admin':
            return True  # Admins have access to all modules
        module_map = {
            'inspection': (
                self.access_hvac or self.access_civil or self.access_cleaning
            ),
            'hvac_mep': self.access_hvac,
            'civil': self.access_civil,
            'cleaning': self.access_cleaning,
            'hr': getattr(self, 'access_hr', False),
            'hiring': self.has_hiring_submodule(),
            'procurement_module': getattr(self, 'access_procurement_module', False),
            'business_development': self.is_bd_inspection_reviewer(),
            'mmr': bool(getattr(self, 'access_report_generation', False)),
            'submitted_forms': bool(getattr(self, 'access_submitted_forms', False)),
            'ticketing': bool(getattr(self, 'access_ticketing', False)),
            'qhsi': bool(getattr(self, 'access_qhsi', False)),
            'files': bool(getattr(self, 'access_files', False)),
        }
        return module_map.get(module, False)

    def has_hiring_submodule(self):
        """Hiring docs / leave tracker / manpower — nested under HR in admin Module access."""
        if self.role == 'admin':
            return True
        return bool(getattr(self, 'access_hiring', False))

    def is_bd_inspection_reviewer(self):
        """BD reviewer lanes on inspection forms and BD email (designation BD, or access flag if not a conflicting primary role)."""
        if self.role == 'admin':
            return False
        d = (self.designation or '').strip().lower()
        if d == 'business_development':
            return True
        if not bool(getattr(self, 'access_business_development', False)):
            return False
        priority = {
            'supervisor', 'operations_manager', 'procurement', 'general_manager',
            'hr_manager', 'hr',
        }
        return d not in priority
    
    def to_dict(self, include_sensitive=False):
        """Convert to dictionary"""
        data = {
            'id': self.id,
            'username': self.username,
            'email': self.email,
            'full_name': self.full_name,
            'role': self.role,
            'is_active': self.is_active,
            'access_hvac': self.access_hvac if self.role != 'admin' else True,
            'access_civil': self.access_civil if self.role != 'admin' else True,
            'access_cleaning': self.access_cleaning if self.role != 'admin' else True,
            'access_hr': getattr(self, 'access_hr', False) if self.role != 'admin' else True,
            'access_hiring': self.has_hiring_submodule() if self.role != 'admin' else True,
            'access_procurement_module': getattr(self, 'access_procurement_module', False) if self.role != 'admin' else True,
            'access_business_development': getattr(self, 'access_business_development', False) if self.role != 'admin' else True,
            'access_sales_manager': getattr(self, 'access_sales_manager', False) if self.role != 'admin' else True,
            'access_quotations': getattr(self, 'access_quotations', False) if self.role != 'admin' else True,
            'access_report_generation': getattr(self, 'access_report_generation', False) if self.role != 'admin' else True,
            'access_submitted_forms': getattr(self, 'access_submitted_forms', False) if self.role != 'admin' else True,
            'access_ticketing': getattr(self, 'access_ticketing', False) if self.role != 'admin' else True,
            'access_qhsi': getattr(self, 'access_qhsi', False) if self.role != 'admin' else True,
            'access_files': getattr(self, 'access_files', False) if self.role != 'admin' else True,
            'is_ticket_reporter': getattr(self, 'is_ticket_reporter', False),
            'password_changed': self.password_changed if hasattr(self, 'password_changed') else True,
            'mfa_enabled': bool(getattr(self, 'mfa_enabled', False)),
            'mfa_configured': bool(getattr(self, 'mfa_secret', None)),
            'designation': self.designation if hasattr(self, 'designation') else None,
            'default_signature': self.default_signature if hasattr(self, 'default_signature') else None,
            'default_comment': self.default_comment if hasattr(self, 'default_comment') else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_login': self.last_login.isoformat() if self.last_login else None,
            'employment_start_date': self.employment_start_date.isoformat() if getattr(self, 'employment_start_date', None) else None,
            'job_designation': getattr(self, 'job_designation', None),
            'annual_leave_days': getattr(self, 'annual_leave_days', None),
            'other_leave_days': getattr(self, 'other_leave_days', None),
            'reporting_manager_id': getattr(self, 'reporting_manager_id', None),
            'operations_manager_id': getattr(self, 'operations_manager_id', None),
            'phone': getattr(self, 'phone', None),
            'assigned_project': getattr(self, 'assigned_project', None),
        }
        mgr = getattr(self, 'reporting_manager', None)
        if mgr:
            data['reporting_manager'] = {
                'id': mgr.id,
                'username': mgr.username,
                'email': mgr.email,
                'full_name': mgr.full_name,
            }
        else:
            data['reporting_manager'] = None
        om = getattr(self, 'operations_manager', None)
        if om:
            data['operations_manager'] = {
                'id': om.id,
                'username': om.username,
                'email': om.email,
                'full_name': om.full_name,
                'designation': getattr(om, 'designation', None),
            }
        else:
            data['operations_manager'] = None
        if include_sensitive:
            data['password_stored'] = False
        return data
    
    def to_client_dict(self):
        """Session/API user payload including DocHub access (stored in dochub_access, not on User)."""
        data = self.to_dict()
        if self.role == 'admin':
            data['can_access_dochub'] = True
        else:
            row = DocHubAccess.query.filter_by(user_id=self.id).first()
            data['can_access_dochub'] = row.can_access if row else True
        return data
    
    def __repr__(self):
        return f'<User {self.username}>'


class Submission(db.Model):
    """Form submissions from all modules"""
    __tablename__ = 'submissions'
    
    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.String(50), unique=True, nullable=False, index=True)
    doc_number = db.Column(db.String(20), nullable=True, index=True)  # Human-facing series number, e.g. 'HR-0001', 'INSP-0042'
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    module_type = db.Column(db.String(50), nullable=False, index=True)  # 'hvac_mep', 'civil', 'cleaning'
    site_name = db.Column(db.String(255))
    visit_date = db.Column(db.Date)
    status = db.Column(db.String(20), default='draft', index=True)  # 'draft', 'submitted', 'processing', 'completed'
    workflow_status = db.Column(db.String(40), default='submitted', index=True)  # 'submitted', 'operations_manager_review', 'operations_manager_approved', 'bd_procurement_review', 'bd_approved', 'procurement_approved', 'general_manager_review', 'general_manager_approved', 'completed', 'rejected'
    
    # Workflow participants
    supervisor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)  # Original submitter
    operations_manager_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    business_dev_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    procurement_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    general_manager_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    
    # Legacy fields (kept for backwards compatibility, deprecated)
    manager_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)  # Deprecated - use operations_manager_id
    supervisor_notified_at = db.Column(db.DateTime, nullable=True)  # Deprecated
    supervisor_reviewed_at = db.Column(db.DateTime, nullable=True)  # Deprecated
    manager_notified_at = db.Column(db.DateTime, nullable=True)  # Deprecated
    manager_reviewed_at = db.Column(db.DateTime, nullable=True)  # Deprecated
    
    # New workflow timestamps
    operations_manager_notified_at = db.Column(db.DateTime, nullable=True)
    operations_manager_approved_at = db.Column(db.DateTime, nullable=True)
    business_dev_notified_at = db.Column(db.DateTime, nullable=True)
    business_dev_approved_at = db.Column(db.DateTime, nullable=True)
    procurement_notified_at = db.Column(db.DateTime, nullable=True)
    procurement_approved_at = db.Column(db.DateTime, nullable=True)
    general_manager_notified_at = db.Column(db.DateTime, nullable=True)
    general_manager_approved_at = db.Column(db.DateTime, nullable=True)
    
    # Approval comments and signatures
    operations_manager_comments = db.Column(db.Text, nullable=True)
    business_dev_comments = db.Column(db.Text, nullable=True)
    procurement_comments = db.Column(db.Text, nullable=True)
    general_manager_comments = db.Column(db.Text, nullable=True)
    
    # Rejection tracking
    rejection_stage = db.Column(db.String(40), nullable=True)  # Which stage rejected
    rejection_reason = db.Column(db.Text, nullable=True)
    rejected_at = db.Column(db.DateTime, nullable=True)
    rejected_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    form_data = db.Column(JSON, nullable=False)  # All form fields as JSON
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    # Relationships
    jobs = db.relationship('Job', backref='submission', lazy='dynamic', cascade='all, delete-orphan')
    files = db.relationship('File', backref='submission', lazy='dynamic', cascade='all, delete-orphan')
    
    def to_dict(self, include_form_data=True, include_latest_job=True):
        """Convert to dictionary.
        For list/history endpoints use include_form_data=False, include_latest_job=False to avoid
        huge JSON payloads and N+1 Job queries.
        """
        latest_job = None
        if include_latest_job:
            try:
                if hasattr(self, 'jobs'):
                    completed_jobs = [j for j in self.jobs if hasattr(j, 'status') and j.status == 'completed']
                    if completed_jobs:
                        latest_job = max(completed_jobs, key=lambda j: j.completed_at if (hasattr(j, 'completed_at') and j.completed_at) else datetime.min)
            except Exception:
                pass

        data = {
            'id': self.id,
            'submission_id': self.submission_id,
            'user_id': self.user_id,
            'module_type': self.module_type,
            'module': self.module_type,  # Alias for frontend compatibility
            'site_name': self.site_name,
            'visit_date': self.visit_date.isoformat() if self.visit_date else None,
            'status': self.status,
            'workflow_status': getattr(self, 'workflow_status', 'submitted'),
            'supervisor_id': getattr(self, 'supervisor_id', None),
            'operations_manager_id': getattr(self, 'operations_manager_id', None),
            'business_dev_id': getattr(self, 'business_dev_id', None),
            'procurement_id': getattr(self, 'procurement_id', None),
            'general_manager_id': getattr(self, 'general_manager_id', None),
            'manager_id': getattr(self, 'manager_id', None),
            'rejection_reason': getattr(self, 'rejection_reason', None),
            'rejected_at': naive_utc_isoformat_z(getattr(self, 'rejected_at', None)) if hasattr(self, 'rejected_at') and getattr(self, 'rejected_at', None) else None,
            'supervisor_notified_at': naive_utc_isoformat_z(getattr(self, 'supervisor_notified_at', None)) if hasattr(self, 'supervisor_notified_at') and getattr(self, 'supervisor_notified_at', None) else None,
            'supervisor_reviewed_at': naive_utc_isoformat_z(getattr(self, 'supervisor_reviewed_at', None)) if hasattr(self, 'supervisor_reviewed_at') and getattr(self, 'supervisor_reviewed_at', None) else None,
            'manager_notified_at': naive_utc_isoformat_z(getattr(self, 'manager_notified_at', None)) if hasattr(self, 'manager_notified_at') and getattr(self, 'manager_notified_at', None) else None,
            'manager_reviewed_at': naive_utc_isoformat_z(getattr(self, 'manager_reviewed_at', None)) if hasattr(self, 'manager_reviewed_at') and getattr(self, 'manager_reviewed_at', None) else None,
            'operations_manager_approved_at': naive_utc_isoformat_z(getattr(self, 'operations_manager_approved_at', None)) if hasattr(self, 'operations_manager_approved_at') and getattr(self, 'operations_manager_approved_at', None) else None,
            'business_dev_approved_at': naive_utc_isoformat_z(getattr(self, 'business_dev_approved_at', None)) if hasattr(self, 'business_dev_approved_at') and getattr(self, 'business_dev_approved_at', None) else None,
            'procurement_approved_at': naive_utc_isoformat_z(getattr(self, 'procurement_approved_at', None)) if hasattr(self, 'procurement_approved_at') and getattr(self, 'procurement_approved_at', None) else None,
            'general_manager_approved_at': naive_utc_isoformat_z(getattr(self, 'general_manager_approved_at', None)) if hasattr(self, 'general_manager_approved_at') and getattr(self, 'general_manager_approved_at', None) else None,
            'created_at': naive_utc_isoformat_z(self.created_at) if self.created_at else None,
            'updated_at': naive_utc_isoformat_z(self.updated_at) if self.updated_at else None,
            'latest_job_id': latest_job.job_id if latest_job else None  # Latest completed job for downloads
        }
        if include_form_data:
            data['form_data'] = self.form_data
        if include_latest_job:
            pass  # latest_job_id already set above
        else:
            data['latest_job_id'] = None
        return data
    
    def __repr__(self):
        return f'<Submission {self.submission_id} - {self.module_type}>'


class Job(db.Model):
    """Background jobs for report generation"""
    __tablename__ = 'jobs'
    
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.String(50), unique=True, nullable=False, index=True)
    submission_id = db.Column(db.Integer, db.ForeignKey('submissions.id', ondelete='CASCADE'), nullable=False)
    status = db.Column(db.String(20), default='pending', index=True)  # 'pending', 'processing', 'completed', 'failed'
    progress = db.Column(db.Integer, default=0)  # 0-100
    result_data = db.Column(JSON)  # URLs for Excel/PDF, error messages
    error_message = db.Column(db.Text)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=_utcnow)
    
    def to_dict(self):
        """Convert to dictionary"""
        return {
            'id': self.id,
            'job_id': self.job_id,
            'submission_id': self.submission_id,
            'status': self.status,
            'progress': self.progress,
            'result_data': self.result_data,
            'error_message': self.error_message,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
    
    def __repr__(self):
        return f'<Job {self.job_id} - {self.status}>'


class File(db.Model):
    """Uploaded files (photos, signatures, reports)"""
    __tablename__ = 'files'
    
    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.String(50), unique=True, nullable=False)
    submission_id = db.Column(db.Integer, db.ForeignKey('submissions.id', ondelete='CASCADE'), nullable=False)
    file_type = db.Column(db.String(20), index=True)  # 'photo', 'signature', 'report_pdf', 'report_excel'
    filename = db.Column(db.String(255))
    file_path = db.Column(db.String(500))  # Local path or NULL if cloud-only
    cloud_url = db.Column(db.String(500))  # Cloudinary URL
    is_cloud = db.Column(db.Boolean, default=True)
    file_size = db.Column(db.Integer)  # In bytes
    mime_type = db.Column(db.String(100))
    uploaded_at = db.Column(db.DateTime, default=_utcnow)
    
    def to_dict(self):
        """Convert to dictionary"""
        return {
            'id': self.id,
            'file_id': self.file_id,
            'submission_id': self.submission_id,
            'file_type': self.file_type,
            'filename': self.filename,
            'file_path': self.file_path,
            'cloud_url': self.cloud_url,
            'is_cloud': self.is_cloud,
            'file_size': self.file_size,
            'mime_type': self.mime_type,
            'uploaded_at': self.uploaded_at.isoformat() if self.uploaded_at else None
        }
    
    def __repr__(self):
        return f'<File {self.filename} - {self.file_type}>'


class AuditLog(db.Model):
    """Audit trail for security and compliance"""
    __tablename__ = 'audit_logs'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    action = db.Column(db.String(50), nullable=False, index=True)  # 'login', 'logout', 'create_submission', etc.
    resource_type = db.Column(db.String(50))  # 'submission', 'job', 'user'
    resource_id = db.Column(db.String(100))
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.Text)
    details = db.Column(JSON)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)
    
    def to_dict(self):
        """Convert to dictionary"""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'action': self.action,
            'resource_type': self.resource_type,
            'resource_id': self.resource_id,
            'ip_address': self.ip_address,
            'user_agent': self.user_agent,
            'details': self.details,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
    
    def __repr__(self):
        return f'<AuditLog {self.action} - User {self.user_id}>'


class AdminEditOtp(db.Model):
    """Hashed email OTP + short-lived grant to edit an administrator profile."""
    __tablename__ = 'admin_edit_otp'

    id = db.Column(db.Integer, primary_key=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    target_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    code_hash = db.Column(db.String(64), nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    attempt_count = db.Column(db.Integer, default=0, nullable=False)
    grant_expires_at = db.Column(db.DateTime, nullable=True)
    request_count = db.Column(db.Integer, default=0, nullable=False)
    window_started_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        db.UniqueConstraint('actor_user_id', 'target_user_id', name='uq_admin_edit_otp_actor_target'),
    )

    def __repr__(self):
        return f'<AdminEditOtp actor={self.actor_user_id} target={self.target_user_id}>'


class Session(db.Model):
    """JWT session management for token revocation"""
    __tablename__ = 'sessions'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    token_jti = db.Column(db.String(100), unique=True, nullable=False, index=True)  # JWT ID
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    is_revoked = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=_utcnow)
    
    def to_dict(self):
        """Convert to dictionary"""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'token_jti': self.token_jti,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'is_revoked': self.is_revoked,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
    
    def __repr__(self):
        return f'<Session {self.token_jti[:8]}... - User {self.user_id}>'


class Device(db.Model):
    """Registered devices for admin management"""
    __tablename__ = 'devices'

    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String(50), unique=True, nullable=False, index=True)  # e.g. DEV-0001
    name = db.Column(db.String(255), nullable=False)
    device_type = db.Column(db.String(30), default='Laptop')  # Laptop, Desktop, Mobile, Server, Tablet
    os = db.Column(db.String(80), default='Windows 11')  # macOS, Windows 11, iOS, Ubuntu, etc.
    status = db.Column(db.String(20), default='idle', index=True)  # online, offline, idle, update
    health = db.Column(db.Integer, default=100)  # 0-100
    assigned_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    serial_or_asset_tag = db.Column(db.String(100), nullable=True)
    last_active_at = db.Column(db.DateTime, nullable=True)
    building = db.Column(db.String(160), nullable=True, index=True)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    device_comment = db.Column(db.Text, nullable=True)
    asset_owner_name = db.Column(db.String(255), nullable=True)
    assignment_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    assigned_user = db.relationship('User', backref='devices', foreign_keys=[assigned_user_id])
    handovers = db.relationship(
        'DeviceHandover',
        backref='device',
        lazy='dynamic',
        cascade='all, delete-orphan',
    )

    def to_dict(self):
        last = 'Never'
        if self.last_active_at:
            delta = datetime.now(timezone.utc).replace(tzinfo=None) - self.last_active_at
            if delta.days > 0:
                last = f'{delta.days}d ago'
            elif delta.seconds >= 3600:
                last = f'{delta.seconds // 3600}h ago'
            elif delta.seconds >= 60:
                last = f'{delta.seconds // 60}m ago'
            else:
                last = 'Just now'
        assigned = self.assigned_user
        return {
            'id': self.id,
            'device_id': self.device_id,
            'name': self.name,
            'device_type': self.device_type,
            'os': self.os,
            'status': self.status,
            'health': self.health,
            'assigned_user_id': self.assigned_user_id,
            'assigned_user': assigned.email.split('@')[0] if assigned and assigned.email else None,
            'assigned_user_name': assigned.full_name if assigned else None,
            'assigned_user_email': assigned.email if assigned else None,
            'serial_or_asset_tag': self.serial_or_asset_tag,
            'building': self.building,
            'latitude': self.latitude,
            'longitude': self.longitude,
            'device_comment': self.device_comment,
            'asset_owner_name': self.asset_owner_name,
            'assignment_date': self.assignment_date.isoformat() if self.assignment_date else None,
            'last_active': last,
            'last_active_at': self.last_active_at.isoformat() if self.last_active_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }

    def __repr__(self):
        return f'<Device {self.device_id} - {self.name}>'


class DeviceHandover(db.Model):
    """Audit log for device custody transfers."""
    __tablename__ = 'device_handovers'

    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(
        db.Integer,
        db.ForeignKey('devices.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    handover_at = db.Column(db.DateTime, nullable=False, index=True)
    from_person_name = db.Column(db.String(255), nullable=False)
    from_person_email = db.Column(db.String(255), nullable=True)
    from_person_phone = db.Column(db.String(80), nullable=True)
    to_person_name = db.Column(db.String(255), nullable=False)
    to_person_email = db.Column(db.String(255), nullable=True)
    to_person_phone = db.Column(db.String(80), nullable=True)
    condition_rating = db.Column(db.String(20), nullable=False)
    condition_detail = db.Column(db.Text, nullable=True)
    accessories_included = db.Column(db.Text, nullable=True)
    defects_reported = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    recorded_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

    recorded_by = db.relationship('User', foreign_keys=[recorded_by_user_id])

    def to_dict(self):
        device = self.device
        return {
            'id': self.id,
            'device_pk': self.device_id,
            'device_id': device.device_id if device else None,
            'device_name': device.name if device else None,
            'handover_at': self.handover_at.isoformat() if self.handover_at else None,
            'from_person_name': self.from_person_name,
            'from_person_email': self.from_person_email,
            'to_person_name': self.to_person_name,
            'to_person_email': self.to_person_email,
            'condition_rating': self.condition_rating,
            'condition_detail': self.condition_detail,
            'accessories_included': self.accessories_included,
            'defects_reported': self.defects_reported,
            'notes': self.notes,
            'recorded_by_user_id': self.recorded_by_user_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f'<DeviceHandover {self.id} device={self.device_id}>'


class BDProject(db.Model):
    """Business development projects/deals"""
    __tablename__ = 'bd_projects'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, index=True)
    company = db.Column(db.String(255), nullable=False, index=True)
    stage = db.Column(db.String(30), default='prospecting', index=True)  # prospecting, qualifying, proposal, negotiation, closing
    status = db.Column(db.String(20), default='active', index=True)  # active, prospect, proposal, won, lost, under_renewal
    priority = db.Column(db.String(10), default='med')  # high, med, low
    value_amount = db.Column(db.Float, default=0.0)
    progress = db.Column(db.Integer, default=0)
    owner = db.Column(db.String(120), nullable=True)  # display name (legacy / denormalized)
    owner_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    next_action = db.Column(db.String(255), nullable=True)
    expected_close_date = db.Column(db.Date, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    primary_contact_name = db.Column(db.String(120), nullable=True)
    primary_contact_email = db.Column(db.String(255), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    owner_user = db.relationship('User', foreign_keys=[owner_user_id],
                                 backref=db.backref('owned_bd_projects', lazy='dynamic'))

    def to_dict(self):
        value_amount = float(self.value_amount or 0)
        owner_name = None
        if self.owner_user:
            owner_name = self.owner_user.full_name or self.owner_user.username
        owner_name = owner_name or self.owner or 'Unassigned'
        return {
            'id': self.id,
            'name': self.name,
            'co': self.company,
            'company': self.company,
            'icon': '🏢',
            'bg': '#fff4ef',
            'stage': self.stage,
            'status': self.status,
            'priority': self.priority,
            'valueAmount': value_amount,
            'value': f'AED {value_amount:,.0f}',
            'progress': max(0, min(100, int(self.progress or 0))),
            'owner': owner_name,
            'ownerUserId': self.owner_user_id,
            'owner_user_id': self.owner_user_id,
            'next': self.next_action or 'No action',
            'nextDate': self.expected_close_date.isoformat() if self.expected_close_date else '',
            'expectedCloseDate': self.expected_close_date.isoformat() if self.expected_close_date else None,
            'notes': self.notes,
            'primaryContactName': self.primary_contact_name,
            'primaryContactEmail': self.primary_contact_email,
            'createdBy': self.created_by,
            'createdAt': self.created_at.isoformat() if self.created_at else None,
            'updatedAt': self.updated_at.isoformat() if self.updated_at else None
        }

    def __repr__(self):
        return f'<BDProject {self.id} - {self.name}>'


class BDFollowUp(db.Model):
    """Business development follow-up tasks"""
    __tablename__ = 'bd_followups'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    company = db.Column(db.String(255), nullable=True, index=True)
    followup_type = db.Column(db.String(20), default='call')  # call, email, meeting, note
    due_at = db.Column(db.DateTime, nullable=True, index=True)
    status = db.Column(db.String(20), default='open', index=True)  # open, done
    details = db.Column(db.Text, nullable=True)
    project_id = db.Column(db.Integer, db.ForeignKey('bd_projects.id'), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    project = db.relationship('BDProject', backref=db.backref('followups', lazy='dynamic'))

    def to_dict(self):
        icon_map = {'call': '📞', 'email': '📧', 'meeting': '🤝', 'note': '📝'}
        return {
            'id': self.id,
            'icon': icon_map.get(self.followup_type, '📝'),
            'title': self.title,
            'co': self.company or (self.project.company if self.project else ''),
            'date': self.due_at.isoformat() if self.due_at else '',
            'type': self.followup_type,
            'status': self.status,
            'details': self.details,
            'projectId': self.project_id,
            'createdAt': self.created_at.isoformat() if self.created_at else None,
            'updatedAt': self.updated_at.isoformat() if self.updated_at else None
        }

    def __repr__(self):
        return f'<BDFollowUp {self.id} - {self.title}>'


class BDContact(db.Model):
    """Business development contacts"""
    __tablename__ = 'bd_contacts'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, index=True)
    title = db.Column(db.String(120), nullable=True)
    company = db.Column(db.String(255), nullable=True, index=True)
    email = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    tags = db.Column(JSON, default=list)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    def to_dict(self):
        safe_name = (self.name or '').strip()
        initials = ''.join([part[0] for part in safe_name.split() if part])[:2].upper() or 'NA'
        return {
            'id': self.id,
            'initials': initials,
            'name': self.name,
            'title': self.title or 'Contact',
            'co': self.company or '',
            'company': self.company or '',
            'email': self.email,
            'phone': self.phone,
            'tags': self.tags if isinstance(self.tags, list) else [],
            'createdAt': self.created_at.isoformat() if self.created_at else None,
            'updatedAt': self.updated_at.isoformat() if self.updated_at else None
        }

    def __repr__(self):
        return f'<BDContact {self.id} - {self.name}>'


class BDActivity(db.Model):
    """Business development activity timeline"""
    __tablename__ = 'bd_activities'

    id = db.Column(db.Integer, primary_key=True)
    icon = db.Column(db.String(10), default='📝')
    bg = db.Column(db.String(20), default='#fff4ef')
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    badge = db.Column(db.String(120), nullable=True)
    event_time = db.Column(db.DateTime, default=_utcnow, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)

    def to_dict(self):
        return {
            'id': self.id,
            'icon': self.icon or '📝',
            'bg': self.bg or '#fff4ef',
            'title': self.title,
            'desc': self.description or '',
            'badge': self.badge or '',
            'time': self.event_time.isoformat() if self.event_time else None,
            'createdAt': self.created_at.isoformat() if self.created_at else None
        }

    def __repr__(self):
        return f'<BDActivity {self.id} - {self.title}>'


QUOTATION_DEFAULT_INTRO = (
    'With reference to our discussion, we are pleased to quote for the following:'
)
QUOTATION_DEFAULT_NOTES = (
    'VAT excluded in the quote.\n'
    'Prices may change at any time; new prices apply unless the client confirms '
    'and the advance payment is received.'
)
QUOTATION_DEFAULT_EXCLUSIONS = (
    'Any civil work is excluded and remains the client\'s scope.\n'
    'Any additional requirement or variation will be quoted separately.'
)
QUOTATION_DEFAULT_TERMS = (
    'Validity : 10 Days\n'
    'Delivery : as per stock availability at the time of approval and advance payment clearance\n'
    'Payment : 50% advance, 50% after completion\n'
    'Please confirm your acceptance to enable us to proceed, assuring you of our best services at all times.'
)
QUOTATION_DEFAULT_SIGNATORY_NAME = 'Business Development'
QUOTATION_DEFAULT_SIGNATORY_EMAIL = ''
QUOTATION_DEFAULT_SIGNATORY_PHONE = ''
QUOTATION_DEFAULT_SIGNOFF_LABEL = 'Thanks & Regards'


class Quotation(db.Model):
    """Sales quotation / proposal linked to a BD deal."""
    __tablename__ = 'quotations'

    id = db.Column(db.Integer, primary_key=True)
    quote_no = db.Column(db.String(50), unique=True, nullable=False, index=True)
    ref_no = db.Column(db.String(80), nullable=True, index=True)
    bd_project_id = db.Column(db.Integer, db.ForeignKey('bd_projects.id', ondelete='SET NULL'), nullable=True, index=True)
    company_name = db.Column(db.String(255), nullable=False)
    contact_name = db.Column(db.String(160), nullable=True)
    contact_email = db.Column(db.String(255), nullable=True)
    kind_attn = db.Column(db.String(160), nullable=True)
    client_tel = db.Column(db.String(60), nullable=True)
    subject = db.Column(db.String(500), nullable=True)
    project_name = db.Column(db.String(255), nullable=True)
    intro_text = db.Column(db.Text, nullable=True)
    quote_date = db.Column(db.Date, nullable=False, default=lambda: _utcnow().date())
    valid_until = db.Column(db.Date, nullable=True)
    status = db.Column(db.String(30), default='draft', index=True)
    subtotal = db.Column(db.Float, default=0.0)
    discount_amount = db.Column(db.Float, default=0.0)
    tax_pct = db.Column(db.Float, default=5.0)
    tax_amount = db.Column(db.Float, default=0.0)
    grand_total = db.Column(db.Float, default=0.0)
    amount_in_words = db.Column(db.String(400), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    notes_text = db.Column(db.Text, nullable=True)
    exclusions_text = db.Column(db.Text, nullable=True)
    terms_text = db.Column(db.Text, nullable=True)
    signatory_name = db.Column(db.String(160), nullable=True)
    signatory_email = db.Column(db.String(255), nullable=True)
    signatory_phone = db.Column(db.String(60), nullable=True)
    signoff_label = db.Column(db.String(120), nullable=True)
    prepared_signature = db.Column(db.Text, nullable=True)
    submitted_at = db.Column(db.DateTime, nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    approved_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    approval_signature = db.Column(db.Text, nullable=True)
    approval_notes = db.Column(db.Text, nullable=True)
    rejected_at = db.Column(db.DateTime, nullable=True)
    rejection_notes = db.Column(db.Text, nullable=True)
    lpo_filename = db.Column(db.String(255), nullable=True)
    lpo_path = db.Column(db.String(512), nullable=True)
    lpo_cloud_url = db.Column(db.String(512), nullable=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    bd_project = db.relationship('BDProject', foreign_keys=[bd_project_id],
                                 backref=db.backref('quotations', lazy='dynamic'))
    approved_by = db.relationship('User', foreign_keys=[approved_by_id])
    owner_user = db.relationship('User', foreign_keys=[owner_user_id],
                                 backref=db.backref('owned_quotations', lazy='dynamic'))
    created_by = db.relationship('User', foreign_keys=[created_by_id],
                                 backref=db.backref('created_quotations', lazy='dynamic'))
    items = db.relationship('QuotationItem', backref='quotation',
                            cascade='all, delete-orphan', lazy='select',
                            order_by='QuotationItem.id')
    attachments = db.relationship('QuotationAttachment', backref='quotation',
                                  cascade='all, delete-orphan', lazy='select',
                                  order_by='QuotationAttachment.id')

    def to_dict(self, include_items=True):
        data = {
            'id': self.id,
            'quote_no': self.quote_no,
            'ref_no': self.ref_no or self.quote_no,
            'bd_project_id': self.bd_project_id,
            'bd_project_name': self.bd_project.name if self.bd_project else None,
            'company_name': self.company_name,
            'contact_name': self.contact_name,
            'contact_email': self.contact_email,
            'kind_attn': self.kind_attn,
            'client_tel': self.client_tel,
            'subject': self.subject,
            'project_name': self.project_name,
            'intro_text': self.intro_text or QUOTATION_DEFAULT_INTRO,
            'quote_date': self.quote_date.isoformat() if self.quote_date else None,
            'valid_until': self.valid_until.isoformat() if self.valid_until else None,
            'status': self.status,
            'subtotal': self.subtotal,
            'discount_amount': self.discount_amount or 0.0,
            'tax_pct': self.tax_pct,
            'tax_amount': self.tax_amount,
            'grand_total': self.grand_total,
            'amount_in_words': self.amount_in_words,
            'notes': self.notes,
            'notes_text': self.notes_text or QUOTATION_DEFAULT_NOTES,
            'exclusions_text': self.exclusions_text or QUOTATION_DEFAULT_EXCLUSIONS,
            'terms_text': self.terms_text or QUOTATION_DEFAULT_TERMS,
            'signatory_name': self.signatory_name or QUOTATION_DEFAULT_SIGNATORY_NAME,
            'signatory_email': self.signatory_email or QUOTATION_DEFAULT_SIGNATORY_EMAIL,
            'signatory_phone': self.signatory_phone or QUOTATION_DEFAULT_SIGNATORY_PHONE,
            'signoff_label': self.signoff_label or QUOTATION_DEFAULT_SIGNOFF_LABEL,
            'prepared_signature': self.prepared_signature,
            'has_prepared_signature': bool(self.prepared_signature),
            'submitted_at': self.submitted_at.isoformat() if self.submitted_at else None,
            'approved_at': self.approved_at.isoformat() if self.approved_at else None,
            'approved_by_id': self.approved_by_id,
            'approved_by_name': (
                (self.approved_by.full_name or self.approved_by.username)
                if self.approved_by else None
            ),
            'has_approval_signature': bool(self.approval_signature),
            'rejection_notes': self.rejection_notes,
            'lpo_filename': self.lpo_filename,
            'lpo_url': self.lpo_cloud_url or (
                f'/api/admin/bd/quotations/{self.id}/lpo' if self.lpo_path else None
            ),
            'owner_user_id': self.owner_user_id,
            'created_by_id': self.created_by_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'can_prepare': True,
        }
        if include_items:
            data['items'] = [it.to_dict() for it in self.items]
            data['attachments'] = [a.to_dict() for a in self.attachments]
        return data

    def __repr__(self):
        return f'<Quotation {self.quote_no}>'


class QuotationItem(db.Model):
    __tablename__ = 'quotation_items'

    id = db.Column(db.Integer, primary_key=True)
    quotation_id = db.Column(db.Integer, db.ForeignKey('quotations.id', ondelete='CASCADE'),
                             nullable=False, index=True)
    description = db.Column(db.String(255), nullable=False)
    details = db.Column(db.Text, nullable=True)
    quantity = db.Column(db.Float, default=1.0)
    unit = db.Column(db.String(40), nullable=True)
    unit_price = db.Column(db.Float, default=0.0)
    total_price = db.Column(db.Float, default=0.0)

    def to_dict(self):
        return {
            'id': self.id,
            'quotation_id': self.quotation_id,
            'description': self.description,
            'details': self.details,
            'quantity': self.quantity,
            'unit': self.unit,
            'unit_price': self.unit_price,
            'total_price': self.total_price,
        }

    def __repr__(self):
        return f'<QuotationItem {self.id} {self.description}>'


class QuotationAttachment(db.Model):
    """Supporting documents attached to a quotation (besides LPO on header)."""
    __tablename__ = 'quotation_attachments'

    id = db.Column(db.Integer, primary_key=True)
    quotation_id = db.Column(db.Integer, db.ForeignKey('quotations.id', ondelete='CASCADE'),
                             nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(512), nullable=True)
    cloud_url = db.Column(db.String(512), nullable=True)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    uploaded_at = db.Column(db.DateTime, default=_utcnow)

    uploaded_by = db.relationship('User', foreign_keys=[uploaded_by_id])

    def to_dict(self):
        return {
            'id': self.id,
            'quotation_id': self.quotation_id,
            'filename': self.filename,
            'url': self.cloud_url or (
                f'/api/admin/bd/quotations/{self.quotation_id}/attachments/{self.id}'
                if self.file_path else None
            ),
            'uploaded_at': self.uploaded_at.isoformat() if self.uploaded_at else None,
        }

    def __repr__(self):
        return f'<QuotationAttachment {self.id} {self.filename}>'


class AdminPersonalProject(db.Model):
    """Admin-only personal work tracking: current initiatives and metadata."""
    __tablename__ = 'admin_personal_projects'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False, index=True)
    summary = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default='active', index=True)  # planning, active, on_hold, done, archived
    priority = db.Column(db.String(10), default='med')  # low, med, high
    category = db.Column(db.String(80), nullable=True, index=True)
    start_date = db.Column(db.Date, nullable=True)
    target_date = db.Column(db.Date, nullable=True)
    link_url = db.Column(db.String(500), nullable=True)
    tags = db.Column(JSON, default=list)
    notes = db.Column(db.Text, nullable=True)
    is_current_focus = db.Column(db.Boolean, default=False, index=True)
    sort_order = db.Column(db.Integer, default=0, index=True)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    user = db.relationship('User', backref=db.backref('admin_personal_projects', lazy='dynamic'))
    steps = db.relationship(
        'AdminPersonalProgressStep',
        backref='project',
        lazy='dynamic',
        cascade='all, delete-orphan',
        order_by='AdminPersonalProgressStep.sort_order',
    )

    def to_dict(self, include_steps=True):
        tags = self.tags if isinstance(self.tags, list) else []
        out = {
            'id': self.id,
            'title': self.title,
            'summary': self.summary or '',
            'status': self.status or 'active',
            'priority': self.priority or 'med',
            'category': self.category or '',
            'startDate': self.start_date.isoformat() if self.start_date else None,
            'targetDate': self.target_date.isoformat() if self.target_date else None,
            'linkUrl': self.link_url or '',
            'tags': tags,
            'notes': self.notes or '',
            'isCurrentFocus': bool(self.is_current_focus),
            'sortOrder': int(self.sort_order or 0),
            'createdAt': self.created_at.isoformat() if self.created_at else None,
            'updatedAt': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_steps:
            step_rows = self.steps.order_by(AdminPersonalProgressStep.sort_order.asc()).all()
            out['steps'] = [s.to_dict() for s in step_rows]
            done = sum(1 for s in step_rows if (s.status or '') == 'done')
            total = len(step_rows)
            out['progressPercent'] = int(round(100 * done / total)) if total else 0
            out['stepsDone'] = done
            out['stepsTotal'] = total
        return out

    def __repr__(self):
        return f'<AdminPersonalProject {self.id} - {self.title}>'


class AdminPersonalProgressStep(db.Model):
    """Checklist-style steps for a personal admin project."""
    __tablename__ = 'admin_personal_progress_steps'

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('admin_personal_projects.id'), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default='pending', index=True)  # pending, in_progress, done, blocked, skipped
    sort_order = db.Column(db.Integer, default=0, index=True)
    due_date = db.Column(db.Date, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    notes = db.Column(db.Text, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'title': self.title,
            'description': self.description or '',
            'status': self.status or 'pending',
            'sortOrder': int(self.sort_order or 0),
            'dueDate': self.due_date.isoformat() if self.due_date else None,
            'completedAt': self.completed_at.isoformat() + 'Z' if self.completed_at else None,
            'notes': self.notes or '',
        }

    def __repr__(self):
        return f'<AdminPersonalProgressStep {self.id} - {self.title}>'


class DocHubDocument(db.Model):
    """Document metadata for DocHub. Supports both file uploads and editable content docs."""
    __tablename__ = 'dochub_documents'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=True)  # null for content-only docs
    stored_path = db.Column(db.String(500), nullable=True)  # null for content-only docs
    file_type = db.Column(db.String(20), nullable=True, index=True)  # PDF, DOCX, etc.; null for content
    doc_type = db.Column(db.String(20), default='content', index=True)  # 'content' | 'upload'
    content = db.Column(db.Text, nullable=True)  # HTML content for editable docs
    # JSON array: [{ "url": "/api/docs/inline/…", "filename": "…", "feed_document_id": 123 }, …]
    reference_attachments = db.Column(db.Text, nullable=True)
    category = db.Column(db.String(50), default='Internal', index=True)  # onboarding, contracts, policies, manuals, reports, Internal, etc.
    status = db.Column(db.String(20), default='draft', index=True)  # draft, review, published, archived
    size_bytes = db.Column(db.Integer, default=0)
    is_starred = db.Column(db.Boolean, default=False)
    # True when this row mirrors an inline-stored file (editor reference); deleting the row does not delete the file.
    inline_asset = db.Column(db.Boolean, default=False)
    author_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, index=True)

    author = db.relationship('User', backref=db.backref('dochub_documents', lazy='dynamic'))

    def to_dict(self):
        author_name = 'Unknown'
        if self.author:
            author_name = self.author.full_name or self.author.username or 'Unknown'

        size_mb = (self.size_bytes or 0) / (1024 * 1024)
        if size_mb >= 1:
            size_label = f"{size_mb:.1f} MB"
        elif self.size_bytes:
            size_kb = (self.size_bytes or 0) / 1024
            size_label = f"{max(1, int(round(size_kb)))} KB"
        else:
            size_label = '—'

        date_label = self.updated_at.strftime('%b %d, %Y') if self.updated_at else ''

        d = {
            'id': self.id,
            'name': self.title,
            'filename': self.filename or '',
            'path': self.stored_path or '',
            'type': self.file_type or '',
            'doc_type': self.doc_type or 'content',
            'tag': self.category,
            'status': self.status,
            'author': author_name,
            'author_id': self.author_id,
            'date': date_label,
            'dateTs': int(self.updated_at.timestamp()) if self.updated_at else 0,
            'size': size_label,
            'sizeB': int(self.size_bytes or 0),
            'starred': bool(self.is_starred),
            'inline_asset': bool(getattr(self, 'inline_asset', False)),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
        if self.doc_type == 'content':
            d['content'] = self.content or ''
            refs = []
            raw = getattr(self, 'reference_attachments', None)
            if raw:
                try:
                    parsed = json.loads(raw)
                    refs = parsed if isinstance(parsed, list) else []
                except (json.JSONDecodeError, TypeError):
                    refs = []
            d['reference_attachments'] = refs
        return d

    def __repr__(self):
        return f'<DocHubDocument {self.id} - {self.title}>'


class DocHubAccess(db.Model):
    """Per-user access control for DocHub."""
    __tablename__ = 'dochub_access'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False, index=True)
    can_access = db.Column(db.Boolean, default=True)
    updated_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, index=True)

    user = db.relationship(
        'User',
        foreign_keys=[user_id],
        backref=db.backref(
            'dochub_access_entry',
            uselist=False,
            cascade='all, delete-orphan',
            single_parent=True,
        ),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'can_access': bool(self.can_access),
            'updated_by': self.updated_by,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }

    def __repr__(self):
        return f'<DocHubAccess user={self.user_id} access={self.can_access}>'


class KnowledgeBaseEntry(db.Model):
    """Admin-managed knowledge records that feed the Kynvera assistant brain."""
    __tablename__ = 'knowledge_base_entries'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False, index=True)
    content = db.Column(db.Text, nullable=True)  # typed text and/or extracted document text
    keywords = db.Column(db.Text, nullable=True)  # comma-separated search boosters
    category = db.Column(db.String(50), default='General', index=True)
    answer_link = db.Column(db.String(500), nullable=True)  # optional deep link the assistant surfaces
    source_type = db.Column(db.String(20), default='text', index=True)  # 'text' | 'upload' | 'link'
    file_name = db.Column(db.String(255), nullable=True)
    stored_path = db.Column(db.String(500), nullable=True)
    file_type = db.Column(db.String(20), nullable=True)  # PDF, DOCX, TXT, MD
    source_url = db.Column(db.String(1000), nullable=True)  # original URL for 'link' records
    fetched_at = db.Column(db.DateTime, nullable=True)  # when the link was last fetched
    is_active = db.Column(db.Boolean, default=True, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, index=True)

    author = db.relationship('User', foreign_keys=[created_by], backref=db.backref('knowledge_entries', lazy='dynamic'))

    def excerpt(self, length=200):
        text = (self.content or '').strip()
        if len(text) <= length:
            return text
        return text[:length].rsplit(' ', 1)[0] + '…'

    def keyword_list(self):
        if not self.keywords:
            return []
        return [k.strip() for k in self.keywords.split(',') if k.strip()]

    def to_dict(self, include_content=True):
        author_name = None
        if self.author:
            author_name = self.author.full_name or self.author.username
        data = {
            'id': self.id,
            'title': self.title,
            'keywords': self.keyword_list(),
            'category': self.category or 'General',
            'answer_link': self.answer_link or '',
            'source_type': self.source_type or 'text',
            'file_name': self.file_name,
            'file_type': self.file_type,
            'source_url': self.source_url or '',
            'fetched_at': self.fetched_at.isoformat() if self.fetched_at else None,
            'is_active': bool(self.is_active),
            'created_by': self.created_by,
            'author_name': author_name,
            'excerpt': self.excerpt(),
            'content_length': len(self.content or ''),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_content:
            data['content'] = self.content or ''
        return data

    def __repr__(self):
        return f'<KnowledgeBaseEntry {self.id} - {self.title}>'


class MmrChargeableConfig(db.Model):
    """Single-row JSON settings for MMR chargeable rules (admin-editable)."""
    __tablename__ = 'mmr_chargeable_config'

    id = db.Column(db.Integer, primary_key=True)
    config_json = db.Column(JSON, nullable=False)

    def __repr__(self):
        return f'<MmrChargeableConfig id={self.id}>'


class NotificationConfig(db.Model):
    """Single-row JSON settings for workflow notification recipients."""
    __tablename__ = 'notification_config'

    id = db.Column(db.Integer, primary_key=True)
    config_json = db.Column(JSON, nullable=False)

    def __repr__(self):
        return f'<NotificationConfig id={self.id}>'


class EmailLog(db.Model):
    """Outbound email send log for admin review."""
    __tablename__ = 'email_logs'

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True, nullable=False)
    status = db.Column(db.String(16), nullable=False, index=True)  # sent | failed
    source = db.Column(db.String(32), nullable=False, default='other', index=True)
    subject = db.Column(db.String(500), nullable=True)
    to_emails = db.Column(db.Text, nullable=True)
    cc_emails = db.Column(db.Text, nullable=True)
    sent_by_user_id = db.Column(
        db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True
    )
    related_id = db.Column(db.String(120), nullable=True)
    body_preview = db.Column(db.String(500), nullable=True)
    attachment_count = db.Column(db.Integer, default=0)
    error_message = db.Column(db.String(500), nullable=True)

    sent_by = db.relationship('User', foreign_keys=[sent_by_user_id], lazy='select')

    def to_dict(self):
        sent_by_name = None
        try:
            user = self.sent_by
            if user:
                sent_by_name = user.full_name or user.username
        except Exception:
            sent_by_name = None
        return {
            'id': self.id,
            'created_at': naive_utc_isoformat_z(self.created_at),
            'status': self.status,
            'source': self.source,
            'subject': self.subject or '',
            'to_emails': self.to_emails or '',
            'cc_emails': self.cc_emails or '',
            'sent_by_user_id': self.sent_by_user_id,
            'sent_by_name': sent_by_name,
            'related_id': self.related_id,
            'body_preview': self.body_preview or '',
            'attachment_count': self.attachment_count or 0,
            'error_message': self.error_message,
        }

    def __repr__(self):
        return f'<EmailLog {self.id} {self.source} {self.status}>'


class EmailRecipientGroup(db.Model):
    """Saved To/CC recipient groups for BD email (personal or public)."""
    __tablename__ = 'email_recipient_groups'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    emails = db.Column(db.Text, nullable=False, default='')
    scope = db.Column(db.String(16), nullable=False, default='personal', index=True)  # personal | public
    owner_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    owner = db.relationship('User', foreign_keys=[owner_id])

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name or '',
            'emails': self.emails or '',
            'scope': self.scope or 'personal',
            'owner_id': self.owner_id,
            'created_at': naive_utc_isoformat_z(self.created_at),
            'updated_at': naive_utc_isoformat_z(self.updated_at),
        }

    def __repr__(self):
        return f'<EmailRecipientGroup {self.id} {self.name} {self.scope}>'


class EmailAutomation(db.Model):
    """Saved personal or public BD email automation (manual + optional daily schedule)."""
    __tablename__ = 'email_automations'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    scope = db.Column(db.String(16), nullable=False, default='personal', index=True)  # personal | public
    owner_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    to_emails = db.Column(db.Text, nullable=False, default='')
    cc_emails = db.Column(db.Text, nullable=True)
    subject = db.Column(db.String(500), nullable=False, default='')
    body = db.Column(db.Text, nullable=False, default='')
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    schedule_enabled = db.Column(db.Boolean, default=False, nullable=False)
    schedule_hour = db.Column(db.Integer, default=10, nullable=False)
    schedule_minute = db.Column(db.Integer, default=0, nullable=False)
    schedule_paused = db.Column(db.Boolean, default=False, nullable=False)
    last_run_at = db.Column(db.DateTime, nullable=True)
    last_success_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    owner = db.relationship('User', foreign_keys=[owner_id])
    attachments = db.relationship(
        'EmailAutomationAttachment',
        backref='automation',
        cascade='all, delete-orphan',
        lazy='select',
        order_by='EmailAutomationAttachment.sort_order',
    )

    def __repr__(self):
        return f'<EmailAutomation {self.id} {self.name} {self.scope}>'


class EmailAutomationAttachment(db.Model):
    """Attachment slot resolved at send time (Files item, folder latest, or submission reports)."""
    __tablename__ = 'email_automation_attachments'

    id = db.Column(db.Integer, primary_key=True)
    automation_id = db.Column(
        db.Integer, db.ForeignKey('email_automations.id', ondelete='CASCADE'), nullable=False, index=True
    )
    kind = db.Column(db.String(32), nullable=False, default='linked_file')  # linked_file | folder_latest | submission_reports
    files_item_id = db.Column(db.Integer, db.ForeignKey('files_items.id', ondelete='SET NULL'), nullable=True, index=True)
    folder_id = db.Column(db.Integer, db.ForeignKey('files_folders.id', ondelete='SET NULL'), nullable=True, index=True)
    submission_id = db.Column(db.String(64), nullable=True)
    require_new = db.Column(db.Boolean, default=False, nullable=False)
    sort_order = db.Column(db.Integer, default=0, nullable=False)

    files_item = db.relationship('FilesItem', foreign_keys=[files_item_id])
    folder = db.relationship('FilesFolder', foreign_keys=[folder_id])

    def __repr__(self):
        return f'<EmailAutomationAttachment {self.id} {self.kind}>'


class TicketProject(db.Model):
    """Projects managed in ticketing settings"""
    __tablename__ = 'ticket_projects'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    client_name = db.Column(db.String(160), nullable=True)
    description = db.Column(db.Text, nullable=True)
    supervisor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    bd_project_id = db.Column(db.Integer, db.ForeignKey('bd_projects.id', ondelete='SET NULL'), nullable=True, index=True)
    project_end_date = db.Column(db.Date, nullable=True)
    renewal_date = db.Column(db.Date, nullable=True)
    project_value = db.Column(db.Float, nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=_utcnow)

    # Client-side (e.g. municipality) contacts for the closing invoice email.
    # Comma/semicolon-separated address lists — kept simple since this is per-project config,
    # not a full contacts model. Falls back to internal admin/ops recipients when unset.
    finance_emails = db.Column(db.String(500), nullable=True)
    ops_emails = db.Column(db.String(500), nullable=True)

    properties = db.relationship('TicketProperty', backref='project',
                                 lazy='dynamic', cascade='all, delete-orphan',
                                 order_by='TicketProperty.name')
    supervisor_user = db.relationship(
        'User', foreign_keys=[supervisor_id],
        backref=db.backref('ticket_projects_supervised', lazy='dynamic'),
    )
    bd_project = db.relationship('BDProject', foreign_keys=[bd_project_id])

    def to_dict(self, *, with_property_count=False):
        sup = self.supervisor_user
        bp = self.bd_project
        roster = []
        try:
            roster = [
                link.to_dict()
                for link in self.supervisor_links.order_by(TicketProjectSupervisor.id).all()
            ]
        except Exception:
            roster = []
        names = [r.get('name') for r in roster if r.get('name')]
        if names:
            if len(names) == 1:
                summary = names[0]
            else:
                summary = f'{names[0]} + {len(names) - 1} more'
        else:
            summary = None
        d = {
            'id': self.id, 'name': self.name,
            'client_name': self.client_name, 'description': self.description,
            'supervisor_id': self.supervisor_id,
            'supervisor_name': summary or (sup.full_name if sup else None),
            'supervisor_count': len(roster),
            'supervisors': roster,
            'bd_project_id': self.bd_project_id,
            'bd_project_label': (
                f'{bp.name} — {bp.company}' if bp else None
            ),
            'project_end_date': self.project_end_date.isoformat() if self.project_end_date else None,
            'renewal_date': self.renewal_date.isoformat() if self.renewal_date else None,
            'project_value': float(self.project_value) if self.project_value is not None else None,
            'is_active': self.is_active, 'sort_order': self.sort_order,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'finance_emails': self.finance_emails,
            'ops_emails': self.ops_emails,
        }
        if with_property_count:
            d['properties_count'] = self.properties.filter_by(is_active=True).count()
        return d

    def __repr__(self):
        return f'<TicketProject {self.name}>'


def _location_display_label(name, *, area=None, code=None):
    """Human label that keeps duplicate CRM names distinct (name — area (code))."""
    label = (name or '').strip() or 'Untitled'
    area = (area or '').strip()
    code = (code or '').strip()
    if area:
        label = f'{label} — {area}'
    if code:
        label = f'{label} ({code})'
    return label


class TicketProperty(db.Model):
    """Location: Property level (belongs to a project).

    latitude/longitude pin the site on the New Work Order map. Zones and
    sub-zones inherit this pin. Base units may store their own pin and fall
    back to the property when unset.
    CRM exports distinguish sites by Property Code (names can repeat).
    """
    __tablename__ = 'ticket_properties'

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('ticket_projects.id', ondelete='CASCADE'), nullable=True)
    name = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    code = db.Column(db.String(64), nullable=True, unique=True, index=True)
    area = db.Column(db.String(160), nullable=True)
    city = db.Column(db.String(120), nullable=True)
    country = db.Column(db.String(120), nullable=True)
    client_name = db.Column(db.String(160), nullable=True)
    property_type = db.Column(db.String(80), nullable=True)
    criticality = db.Column(db.String(80), nullable=True)
    ownership_type = db.Column(db.String(80), nullable=True)
    plot_no = db.Column(db.String(80), nullable=True)
    external_ref = db.Column(db.String(80), nullable=True)
    status = db.Column(db.String(40), nullable=True)
    initiation_date = db.Column(db.Date, nullable=True)

    zones = db.relationship('TicketZone', backref='property',
                            lazy='dynamic', cascade='all, delete-orphan',
                            order_by='TicketZone.name')

    def display_label(self):
        return _location_display_label(self.name, area=self.area, code=self.code)

    def to_dict(self, with_zones=False):
        d = {
            'id': self.id,
            'name': self.name,
            'label': self.display_label(),
            'project_id': self.project_id,
            'latitude': self.latitude,
            'longitude': self.longitude,
            'code': self.code,
            'area': self.area,
            'city': self.city,
            'country': self.country,
            'client_name': self.client_name,
            'property_type': self.property_type,
            'criticality': self.criticality,
            'ownership_type': self.ownership_type,
            'plot_no': self.plot_no,
            'external_ref': self.external_ref,
            'status': self.status,
            'initiation_date': self.initiation_date.isoformat() if self.initiation_date else None,
        }
        if with_zones:
            d['zones'] = [z.to_dict(with_sub_zones=True) for z in self.zones]
        return d


class TicketZone(db.Model):
    """Location: Zone level"""
    __tablename__ = 'ticket_zones'

    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey('ticket_properties.id', ondelete='CASCADE'), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    code = db.Column(db.String(64), nullable=True, unique=True, index=True)

    sub_zones = db.relationship('TicketSubZone', backref='zone',
                                lazy='dynamic', cascade='all, delete-orphan',
                                order_by='TicketSubZone.name')

    def display_label(self):
        return _location_display_label(self.name, code=self.code)

    def to_dict(self, with_sub_zones=False):
        d = {
            'id': self.id,
            'name': self.name,
            'label': self.display_label(),
            'property_id': self.property_id,
            'code': self.code,
        }
        if with_sub_zones:
            d['sub_zones'] = [s.to_dict(with_units=True) for s in self.sub_zones]
        return d


class TicketSubZone(db.Model):
    """Location: Sub-zone level"""
    __tablename__ = 'ticket_sub_zones'

    id = db.Column(db.Integer, primary_key=True)
    zone_id = db.Column(db.Integer, db.ForeignKey('ticket_zones.id', ondelete='CASCADE'), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    code = db.Column(db.String(64), nullable=True, unique=True, index=True)

    base_units = db.relationship('TicketBaseUnit', backref='sub_zone',
                                 lazy='dynamic', cascade='all, delete-orphan',
                                 order_by='TicketBaseUnit.name')

    def display_label(self):
        return _location_display_label(self.name, code=self.code)

    def to_dict(self, with_units=False):
        d = {
            'id': self.id,
            'name': self.name,
            'label': self.display_label(),
            'zone_id': self.zone_id,
            'code': self.code,
        }
        if with_units:
            d['base_units'] = [u.to_dict() for u in self.base_units]
        return d


class TicketBaseUnit(db.Model):
    """Location: Base unit (apartment, room, office, etc.).

    Optional latitude/longitude pin this unit on the New Work Order map.
    When unset, the parent property pin is used.
    """
    __tablename__ = 'ticket_base_units'

    id = db.Column(db.Integer, primary_key=True)
    sub_zone_id = db.Column(db.Integer, db.ForeignKey('ticket_sub_zones.id', ondelete='CASCADE'), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    code = db.Column(db.String(64), nullable=True, unique=True, index=True)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)

    def display_label(self):
        return _location_display_label(self.name, code=self.code)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'label': self.display_label(),
            'sub_zone_id': self.sub_zone_id,
            'code': self.code,
            'latitude': self.latitude,
            'longitude': self.longitude,
        }


class TicketTitleTemplate(db.Model):
    """Predefined title templates for quick ticket creation"""
    __tablename__ = 'ticket_title_templates'

    id = db.Column(db.Integer, primary_key=True)
    service_group = db.Column(db.String(120), nullable=True)   # if None → applies to all
    category = db.Column(db.String(120), nullable=True)
    fault_type = db.Column(db.String(120), nullable=True)
    title = db.Column(db.String(255), nullable=False)
    description_template = db.Column(db.Text, nullable=True)   # auto-fill for work description
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=_utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'service_group': self.service_group,
            'category': self.category,
            'fault_type': self.fault_type,
            'title': self.title,
            'description_template': self.description_template,
            'is_active': self.is_active,
        }

    def __repr__(self):
        return f'<TicketTitleTemplate "{self.title}">'


class TicketSupervisorTeam(db.Model):
    """Maps supervisors to their technician team members for ticket assignment."""
    __tablename__ = 'ticket_supervisor_teams'

    id = db.Column(db.Integer, primary_key=True)
    supervisor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    technician_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

    __table_args__ = (
        db.UniqueConstraint('supervisor_id', 'technician_id', name='uq_sup_tech_member'),
    )

    sup_user  = db.relationship('User', foreign_keys=[supervisor_id],
                                backref=db.backref('supervisor_team_entries', lazy='dynamic'))
    tech_user = db.relationship('User', foreign_keys=[technician_id],
                                backref=db.backref('technician_team_entries', lazy='dynamic'))

    def to_dict(self):
        return {
            'id': self.id,
            'supervisor_id': self.supervisor_id,
            'technician_id': self.technician_id,
            'technician_name': self.tech_user.full_name if self.tech_user else None,
            'technician_username': self.tech_user.username if self.tech_user else None,
            'is_active': self.is_active,
        }

    def __repr__(self):
        return f'<TicketSupervisorTeam sup={self.supervisor_id} tech={self.technician_id}>'


class TicketProjectSupervisor(db.Model):
    """Project-scoped supervisor roster (Resource Management)."""
    __tablename__ = 'ticket_project_supervisors'

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('ticket_projects.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

    __table_args__ = (
        db.UniqueConstraint('project_id', 'user_id', name='uq_tkt_project_supervisor'),
    )

    project = db.relationship('TicketProject', backref=db.backref('supervisor_links', lazy='dynamic', cascade='all, delete-orphan'))
    user = db.relationship('User', foreign_keys=[user_id])

    def to_dict(self):
        u = self.user
        return {
            'id': self.id,
            'project_id': self.project_id,
            'user_id': self.user_id,
            'name': (u.full_name or u.username) if u else None,
            'username': u.username if u else None,
            'designation': getattr(u, 'designation', None) if u else None,
        }


class TicketProjectTeamMember(db.Model):
    """Project-scoped team members (technicians) for ticket assignment."""
    __tablename__ = 'ticket_project_team_members'

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('ticket_projects.id', ondelete='CASCADE'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

    __table_args__ = (
        db.UniqueConstraint('project_id', 'user_id', name='uq_tkt_project_team_member'),
    )

    project = db.relationship('TicketProject', backref=db.backref('team_links', lazy='dynamic', cascade='all, delete-orphan'))
    user = db.relationship('User', foreign_keys=[user_id])

    def to_dict(self):
        u = self.user
        speciality = ''
        if u:
            speciality = (getattr(u, 'job_designation', None) or '').strip() or 'Technician'
        return {
            'id': self.id,
            'project_id': self.project_id,
            'user_id': self.user_id,
            'name': (u.full_name or u.username) if u else None,
            'username': u.username if u else None,
            'speciality': speciality,
        }


class TicketVendor(db.Model):
    """Vendor company that can be attached to ticketing projects."""
    __tablename__ = 'ticket_vendors'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    contact_name = db.Column(db.String(160), nullable=True)
    contact_email = db.Column(db.String(255), nullable=True)
    contact_phone = db.Column(db.String(80), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

    technicians = db.relationship(
        'TicketVendorTechnician', backref='vendor',
        lazy='dynamic', cascade='all, delete-orphan',
        order_by='TicketVendorTechnician.name',
    )
    project_links = db.relationship(
        'TicketProjectVendor', backref='vendor',
        lazy='dynamic', cascade='all, delete-orphan',
    )

    def to_dict(self, *, with_technicians=True):
        d = {
            'id': self.id,
            'name': self.name,
            'contact_name': self.contact_name,
            'contact_email': self.contact_email,
            'contact_phone': self.contact_phone,
            'notes': self.notes,
            'is_active': self.is_active,
        }
        if with_technicians:
            techs = [t.to_dict() for t in self.technicians]
            d['technicians'] = techs
            d['technician_count'] = len(techs)
        return d

    def to_assign_dict(self):
        """Shape expected by ticket detail vendor picker."""
        techs = []
        for t in self.technicians:
            techs.append({
                'code': t.code or f'VEND-TECH-{t.id}',
                'name': t.name,
                'speciality': t.speciality or 'Technician',
                'user_id': t.user_id,
            })
        return {
            'id': str(self.id),
            'name': self.name,
            'technicians': techs,
        }


class TicketVendorTechnician(db.Model):
    """Named technician on a vendor company (optional login user)."""
    __tablename__ = 'ticket_vendor_technicians'

    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('ticket_vendors.id', ondelete='CASCADE'), nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False)
    speciality = db.Column(db.String(120), nullable=True)
    code = db.Column(db.String(64), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)

    user = db.relationship('User', foreign_keys=[user_id])

    def to_dict(self):
        return {
            'id': self.id,
            'vendor_id': self.vendor_id,
            'name': self.name,
            'speciality': self.speciality,
            'code': self.code,
            'user_id': self.user_id,
        }


class TicketProjectVendor(db.Model):
    """Attaches a vendor company to a ticketing project."""
    __tablename__ = 'ticket_project_vendors'

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('ticket_projects.id', ondelete='CASCADE'), nullable=False, index=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('ticket_vendors.id', ondelete='CASCADE'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

    __table_args__ = (
        db.UniqueConstraint('project_id', 'vendor_id', name='uq_tkt_project_vendor'),
    )

    project = db.relationship('TicketProject', backref=db.backref('vendor_links', lazy='dynamic', cascade='all, delete-orphan'))


class TicketServiceGroup(db.Model):
    """Configurable ticket service group (Ticket Fields)."""
    __tablename__ = 'ticket_service_groups'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)

    categories = db.relationship(
        'TicketFaultCategory', backref='service_group',
        lazy='dynamic', cascade='all, delete-orphan',
        order_by='TicketFaultCategory.sort_order, TicketFaultCategory.name',
    )

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'sort_order': self.sort_order or 0,
            'is_active': self.is_active,
        }


class TicketFaultCategory(db.Model):
    """Fault category under a service group."""
    __tablename__ = 'ticket_fault_categories'

    id = db.Column(db.Integer, primary_key=True)
    service_group_id = db.Column(
        db.Integer, db.ForeignKey('ticket_service_groups.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    name = db.Column(db.String(120), nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)

    fault_codes = db.relationship(
        'TicketFaultCode', backref='category',
        lazy='dynamic', cascade='all, delete-orphan',
        order_by='TicketFaultCode.sort_order, TicketFaultCode.code',
    )

    def to_dict(self):
        return {
            'id': self.id,
            'service_group_id': self.service_group_id,
            'name': self.name,
            'sort_order': self.sort_order or 0,
            'is_active': self.is_active,
        }


class TicketFaultCode(db.Model):
    """Individual fault code under a category."""
    __tablename__ = 'ticket_fault_codes'

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(
        db.Integer, db.ForeignKey('ticket_fault_categories.id', ondelete='CASCADE'),
        nullable=False, index=True,
    )
    code = db.Column(db.String(64), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    duration_mins = db.Column(db.Integer, nullable=True)
    suggested_title = db.Column(db.String(255), nullable=True)
    suggested_work_description = db.Column(db.Text, nullable=True)
    root_cause_applicability = db.Column(db.String(255), nullable=True)
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)

    def fault_pick_value(self):
        code = (self.code or '').strip()
        name = (self.name or '').strip()
        if code and name:
            return f'{code}: {name}'[:512]
        return (code or name)[:512]

    def to_dict(self):
        cat = self.category
        sg = cat.service_group if cat else None
        pick = self.fault_pick_value()
        name = (self.name or '').strip()
        cat_name = cat.name if cat else ''
        sg_name = sg.name if sg else ''
        return {
            'id': self.id,
            'category_id': self.category_id,
            'catalog_id': self.id,
            'fault_code': self.code,
            'fault_code_name': self.name,
            'fault_category': cat_name,
            'service_group': sg_name,
            'duration_mins': self.duration_mins,
            'root_cause_applicability': self.root_cause_applicability,
            'fault_pick_value': pick,
            'search_label': f'{name} · {cat_name} · {sg_name}' if name else f'{cat_name} · {sg_name}',
            'suggested_title': self.suggested_title or '',
            'suggested_work_description': self.suggested_work_description or '',
            'sort_order': self.sort_order or 0,
            'is_active': self.is_active,
        }


class TicketPriority(db.Model):
    """Configurable ticket priority (value slug is immutable after create)."""
    __tablename__ = 'ticket_priorities'

    id = db.Column(db.Integer, primary_key=True)
    value = db.Column(db.String(40), nullable=False, unique=True)
    label = db.Column(db.String(80), nullable=False)
    sla_hint = db.Column(db.String(80), nullable=True)
    hint = db.Column(db.String(255), nullable=True)
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)

    def to_dict(self):
        return {
            'id': self.id,
            'value': self.value,
            'label': self.label,
            'sla_hint': self.sla_hint,
            'hint': self.hint,
            'sort_order': self.sort_order or 0,
            'is_active': self.is_active,
        }


class TicketHoldReason(db.Model):
    __tablename__ = 'ticket_hold_reasons'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(60), nullable=False, unique=True)
    label = db.Column(db.String(160), nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)

    def to_dict(self):
        return {
            'id': self.id,
            'key': self.key,
            'label': self.label,
            'sort_order': self.sort_order or 0,
            'is_active': self.is_active,
        }


class TicketCancelReason(db.Model):
    __tablename__ = 'ticket_cancel_reasons'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(60), nullable=False, unique=True)
    label = db.Column(db.String(160), nullable=False)
    sort_order = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)

    def to_dict(self):
        return {
            'id': self.id,
            'key': self.key,
            'label': self.label,
            'sort_order': self.sort_order or 0,
            'is_active': self.is_active,
        }


class Ticket(db.Model):
    """Work order / complaint tickets"""
    __tablename__ = 'tickets'

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.String(50), unique=True, nullable=False, index=True)  # TKT-XXXXXXXX

    # Reporter & assignment
    reporter_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    assigned_to_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    # Supervisor workflow
    supervisor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    technician_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    # Classification
    project = db.Column(db.String(160), nullable=False)
    service_group = db.Column(db.String(120), nullable=False)
    category = db.Column(db.String(120), nullable=False)
    fault_type = db.Column(db.String(120), nullable=False)
    priority = db.Column(db.String(20), nullable=False, default='medium')  # low, medium, high, critical

    # Description
    title = db.Column(db.String(255), nullable=False)
    work_description = db.Column(db.Text, nullable=False)

    # Location (name snapshots for history/email; FKs for cascade when names repeat)
    property_name = db.Column(db.String(255), nullable=True)
    zone = db.Column(db.String(255), nullable=True)
    sub_zone = db.Column(db.String(255), nullable=True)
    base_unit = db.Column(db.String(255), nullable=True)
    property_id = db.Column(db.Integer, db.ForeignKey('ticket_properties.id', ondelete='SET NULL'), nullable=True, index=True)
    zone_id = db.Column(db.Integer, db.ForeignKey('ticket_zones.id', ondelete='SET NULL'), nullable=True, index=True)
    sub_zone_id = db.Column(db.Integer, db.ForeignKey('ticket_sub_zones.id', ondelete='SET NULL'), nullable=True, index=True)
    base_unit_id = db.Column(db.Integer, db.ForeignKey('ticket_base_units.id', ondelete='SET NULL'), nullable=True, index=True)

    # Financial
    is_chargeable = db.Column(db.Boolean, default=False)
    projected_cost = db.Column(db.Float, nullable=True)
    total_cost = db.Column(db.Float, nullable=True)

    # Pricing (supervisor sets markup before closing)
    overhead_pct = db.Column(db.Float, default=10.0)      # legacy/unused — kept to avoid a migration
    markup_pct = db.Column(db.Float, nullable=True)        # 0 / 5 / 10 / 15 / 20 / 25
    actual_price = db.Column(db.Float, nullable=True)      # mp + mat (no overhead applied)
    selling_price = db.Column(db.Float, nullable=True)     # actual_price * (1 + markup/100)

    # Narrative fields
    service_report_notes = db.Column(db.Text, nullable=True)          # supervisor's service-report narrative
    technician_resolution_notes = db.Column(db.Text, nullable=True)   # technician's completion notes
    supervisor_verification_notes = db.Column(db.Text, nullable=True) # supervisor's verification remarks

    # Status: open → pending_supervisor → in_progress → pending_parts → pending_verification → closed
    status = db.Column(db.String(30), default='open', index=True)

    # Hold / cancel / timing (v2 workflow — used by ticketing routes + templates)
    on_hold_reason = db.Column(db.Text, nullable=True)
    cancelled_reason = db.Column(db.Text, nullable=True)
    cancelled_at = db.Column(db.Text, nullable=True)
    previous_status = db.Column(db.Text, nullable=True)
    site_attended_at = db.Column(db.Text, nullable=True)
    work_started_at = db.Column(db.Text, nullable=True)
    work_completed_at = db.Column(db.Text, nullable=True)

    # Closing info — supervisor verification (`supervisor-close`)
    close_notes = db.Column(db.Text, nullable=True)
    close_signature = db.Column(db.Text, nullable=True)   # base64 data-URL
    close_signed_by = db.Column(db.String(160), nullable=True)
    close_signed_role = db.Column(db.String(120), nullable=True)

    # Closing info — leftover ops sign-off (`ops-close`) for tickets already provider_closed
    ops_close_notes = db.Column(db.Text, nullable=True)
    ops_close_signature = db.Column(db.Text, nullable=True)   # base64 data-URL
    ops_close_signed_by = db.Column(db.String(160), nullable=True)
    ops_close_signed_role = db.Column(db.String(120), nullable=True)

    # Timestamps
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)
    resolved_at = db.Column(db.DateTime, nullable=True)
    closed_at = db.Column(db.DateTime, nullable=True)

    # Email intake (draft tickets created from inbound email; see TicketEmailIntake)
    source = db.Column(db.String(20), default='manual', index=True)  # 'manual', 'email'
    source_sender_email = db.Column(db.String(255), nullable=True)   # raw From address, even if matched to a User
    source_sender_name = db.Column(db.String(255), nullable=True)    # raw From display name
    source_subject = db.Column(db.String(500), nullable=True)        # original email subject
    source_message_id = db.Column(db.String(255), nullable=True, index=True)  # inbound Message-Id, for de-dupe

    # FM asset link + AI triage SLA (nullable — not all tickets are asset-linked)
    asset_id = db.Column(db.Integer, db.ForeignKey('fm_assets.id'), nullable=True, index=True)
    sla_hours = db.Column(db.Integer, nullable=True)

    # Relationships
    reporter = db.relationship('User', foreign_keys=[reporter_id],
                               backref=db.backref('reported_tickets', lazy='dynamic'))
    assigned_to = db.relationship('User', foreign_keys=[assigned_to_id],
                                  backref=db.backref('assigned_tickets', lazy='dynamic'))
    supervisor = db.relationship('User', foreign_keys=[supervisor_id],
                                 backref=db.backref('supervised_tickets', lazy='dynamic'))
    technician = db.relationship('User', foreign_keys=[technician_id],
                                 backref=db.backref('technician_tickets', lazy='dynamic'))
    fm_asset = db.relationship('Asset', foreign_keys=[asset_id],
                               backref=db.backref('tickets', lazy='dynamic'))
    asset_links = db.relationship(
        'TicketAsset',
        back_populates='ticket',
        cascade='all, delete-orphan',
        lazy='selectin',
        order_by='TicketAsset.id',
    )
    notes = db.relationship('TicketNote', backref='ticket',
                            lazy='dynamic', cascade='all, delete-orphan',
                            order_by='TicketNote.created_at')
    images = db.relationship('TicketImage', backref='ticket',
                             lazy='dynamic', cascade='all, delete-orphan')
    materials = db.relationship('TicketMaterial', backref='ticket',
                                lazy='dynamic', cascade='all, delete-orphan')
    manpower = db.relationship('TicketManpower', backref='ticket',
                               lazy='dynamic', cascade='all, delete-orphan')

    def linked_assets_list(self):
        """All linked FM assets (junction first, else legacy primary)."""
        links = list(self.asset_links or [])
        if links:
            out = []
            seen = set()
            for link in links:
                asset = link.asset
                if not asset or asset.id in seen:
                    continue
                seen.add(asset.id)
                out.append(asset)
            return out
        if self.fm_asset:
            return [self.fm_asset]
        return []

    def linked_assets_dict(self):
        return [
            {
                'id': a.id,
                'asset_id': a.asset_id,
                'name': a.name,
                'building': getattr(a, 'building', None),
            }
            for a in self.linked_assets_list()
        ]

    def to_dict(self):
        return {
            'id': self.id,
            'ticket_id': self.ticket_id,
            'title': self.title,
            'project': self.project,
            'service_group': self.service_group,
            'category': self.category,
            'fault_type': self.fault_type,
            'priority': self.priority,
            'status': self.status,
            'work_description': self.work_description,
            'property_name': self.property_name,
            'zone': self.zone,
            'sub_zone': self.sub_zone,
            'base_unit': self.base_unit,
            'property_id': self.property_id,
            'zone_id': self.zone_id,
            'sub_zone_id': self.sub_zone_id,
            'base_unit_id': self.base_unit_id,
            'is_chargeable': self.is_chargeable,
            'projected_cost': self.projected_cost,
            'total_cost': self.total_cost,
            'overhead_pct': self.overhead_pct,
            'markup_pct': self.markup_pct,
            'actual_price': self.actual_price,
            'selling_price': self.selling_price,
            'reporter_id': self.reporter_id,
            'reporter_name': self.reporter.full_name if self.reporter else None,
            'assigned_to_id': self.assigned_to_id,
            'assigned_to_name': self.assigned_to.full_name if self.assigned_to else None,
            'supervisor_id': self.supervisor_id,
            'supervisor_name': self.supervisor.full_name if self.supervisor else None,
            'technician_id': self.technician_id,
            'technician_name': self.technician.full_name if self.technician else None,
            'asset_id': self.asset_id,
            'asset_code': self.fm_asset.asset_id if self.fm_asset else None,
            'asset_name': self.fm_asset.name if self.fm_asset else None,
            'linked_assets': self.linked_assets_dict(),
            'sla_hours': self.sla_hours,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'closed_at': self.closed_at.isoformat() if self.closed_at else None,
            'source': self.source or 'manual',
            'source_sender_email': self.source_sender_email,
            'source_sender_name': self.source_sender_name,
            'source_subject': self.source_subject,
        }

    def __repr__(self):
        return f'<Ticket {self.ticket_id} [{self.status}]>'


class TicketAsset(db.Model):
    """Many-to-many link between work orders and FM assets."""
    __tablename__ = 'ticket_assets'
    __table_args__ = (
        db.UniqueConstraint('ticket_id', 'asset_pk', name='uq_ticket_asset'),
    )

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('tickets.id', ondelete='CASCADE'), nullable=False, index=True)
    asset_pk = db.Column(db.Integer, db.ForeignKey('fm_assets.id', ondelete='CASCADE'), nullable=False, index=True)
    is_primary = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)

    ticket = db.relationship('Ticket', back_populates='asset_links')
    asset = db.relationship('Asset', backref=db.backref('ticket_links', lazy='dynamic'))

    def __repr__(self):
        return f'<TicketAsset ticket={self.ticket_id} asset={self.asset_pk}>'


class Asset(db.Model):
    """Facility Management equipment/asset registry (chillers, pumps, AHUs, etc.).

    Distinct from Device (IT hardware) and HR asset handover forms.
    """
    __tablename__ = 'fm_assets'

    id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.String(40), unique=True, nullable=False, index=True)  # AST-0001
    qr_code = db.Column(db.String(120), nullable=True, index=True)
    name = db.Column(db.String(200), nullable=False)
    asset_type = db.Column(db.String(120), nullable=True, index=True)  # chiller, pump, AHU, etc.
    building = db.Column(db.String(160), nullable=True, index=True)
    floor = db.Column(db.String(80), nullable=True)
    room = db.Column(db.String(80), nullable=True)
    project_id = db.Column(
        db.Integer,
        db.ForeignKey('ticket_projects.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
    )
    manufacturer = db.Column(db.String(160), nullable=True)
    model = db.Column(db.String(160), nullable=True)
    serial_number = db.Column(db.String(160), nullable=True, index=True)
    installation_date = db.Column(db.Date, nullable=True)
    warranty_expiry = db.Column(db.Date, nullable=True)
    purchase_cost = db.Column(db.Float, nullable=True)
    maintenance_cost_total = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(40), default='active', index=True)  # active, inactive, critical, decommissioned
    health_score = db.Column(db.Integer, nullable=True)  # 0-100
    image_urls = db.Column(db.Text, nullable=True)  # JSON list of URLs
    notes = db.Column(db.Text, nullable=True)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    project = db.relationship('TicketProject', foreign_keys=[project_id])

    def image_list(self):
        if not self.image_urls:
            return []
        try:
            data = json.loads(self.image_urls)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def to_dict(self):
        return {
            'id': self.id,
            'asset_id': self.asset_id,
            'qr_code': self.qr_code,
            'name': self.name,
            'asset_type': self.asset_type,
            'building': self.building,
            'floor': self.floor,
            'room': self.room,
            'project_id': self.project_id,
            'project_name': self.project.name if self.project else None,
            'manufacturer': self.manufacturer,
            'model': self.model,
            'serial_number': self.serial_number,
            'installation_date': self.installation_date.isoformat() if self.installation_date else None,
            'warranty_expiry': self.warranty_expiry.isoformat() if self.warranty_expiry else None,
            'purchase_cost': self.purchase_cost,
            'maintenance_cost_total': self.maintenance_cost_total or 0.0,
            'status': self.status or 'active',
            'health_score': self.health_score,
            'image_urls': self.image_list(),
            'notes': self.notes,
            'latitude': self.latitude,
            'longitude': self.longitude,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f'<Asset {self.asset_id} — {self.name}>'


class AssetPrediction(db.Model):
    """Cached Claude (or future model) failure/RUL estimates for an asset."""
    __tablename__ = 'fm_asset_predictions'

    id = db.Column(db.Integer, primary_key=True)
    asset_pk = db.Column(db.Integer, db.ForeignKey('fm_assets.id', ondelete='CASCADE'), nullable=False, index=True)
    failure_probability_pct = db.Column(db.Float, nullable=True)
    rul_days = db.Column(db.Integer, nullable=True)
    predicted_maintenance_cost = db.Column(db.Float, nullable=True)
    recommendation = db.Column(db.String(40), nullable=True)
    justification = db.Column(db.Text, nullable=True)
    method = db.Column(db.String(40), default='llm_estimate')
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)

    asset = db.relationship('Asset', backref=db.backref('predictions', lazy='dynamic', cascade='all, delete-orphan'))

    def to_dict(self):
        return {
            'id': self.id,
            'asset_pk': self.asset_pk,
            'failure_probability_pct': self.failure_probability_pct,
            'rul_days': self.rul_days,
            'predicted_maintenance_cost': self.predicted_maintenance_cost,
            'recommendation': self.recommendation,
            'justification': self.justification,
            'method': self.method or 'llm_estimate',
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class FloorPlan(db.Model):
    """2D digital-twin floor plan per building/floor with hotspot JSON."""
    __tablename__ = 'fm_floor_plans'

    id = db.Column(db.Integer, primary_key=True)
    building = db.Column(db.String(160), nullable=False, index=True)
    floor = db.Column(db.String(80), nullable=True, index=True)
    name = db.Column(db.String(200), nullable=False)
    image_url = db.Column(db.String(512), nullable=False)
    # hotspots: [{room, x_pct, y_pct, asset_ids?, severity?}]
    hotspots = db.Column(JSON, nullable=True)
    project_id = db.Column(
        db.Integer,
        db.ForeignKey('ticket_projects.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
    )
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'building': self.building,
            'floor': self.floor,
            'name': self.name,
            'image_url': self.image_url,
            'project_id': self.project_id,
            'hotspots': self.hotspots or [],
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class IntegrationApiKey(db.Model):
    """API keys for external system access to FM REST endpoints."""
    __tablename__ = 'integration_api_keys'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    key_prefix = db.Column(db.String(12), nullable=False, index=True)
    key_hash = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    last_used_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'key_prefix': self.key_prefix,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_used_at': self.last_used_at.isoformat() if self.last_used_at else None,
        }


class OutboundWebhook(db.Model):
    """Configurable outbound event webhooks (ticket/asset events)."""
    __tablename__ = 'outbound_webhooks'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    target_url = db.Column(db.String(512), nullable=False)
    secret = db.Column(db.String(120), nullable=True)
    events = db.Column(JSON, nullable=True)  # e.g. ["ticket.created","ticket.closed","asset.critical"]
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=_utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'target_url': self.target_url,
            'events': self.events or [],
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class PushDeviceToken(db.Model):
    """FCM/APNs device tokens for push notifications."""
    __tablename__ = 'push_device_tokens'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    token = db.Column(db.String(512), nullable=False, unique=True)
    platform = db.Column(db.String(20), default='android')  # android, ios, web
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    user = db.relationship('User', backref=db.backref('push_tokens', lazy='dynamic', cascade='all, delete-orphan'))


class PortfolioForecast(db.Model):
    """Cached portfolio-level Claude forecast (budget/failure/spares)."""
    __tablename__ = 'fm_portfolio_forecasts'

    id = db.Column(db.Integer, primary_key=True)
    payload = db.Column(JSON, nullable=False)
    method = db.Column(db.String(40), default='llm_estimate')
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)

    def to_dict(self):
        data = dict(self.payload or {})
        data['method'] = self.method or 'llm_estimate'
        data['created_at'] = self.created_at.isoformat() if self.created_at else None
        data['id'] = self.id
        return data


class TicketTriageLog(db.Model):
    """Audit trail for AI ticket triage suggestions vs human decisions."""
    __tablename__ = 'ticket_triage_logs'

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('tickets.id', ondelete='SET NULL'), nullable=True, index=True)
    ticket_code = db.Column(db.String(50), nullable=True, index=True)  # TKT-... when known
    actor_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    prompt_inputs = db.Column(JSON, nullable=True)
    raw_response = db.Column(db.Text, nullable=True)
    suggested = db.Column(JSON, nullable=True)  # priority, sla_hours, technician_id, required_parts, reasoning
    accepted = db.Column(JSON, nullable=True)   # final human decision (or null if preview-only)
    decision = db.Column(db.String(40), default='preview')  # preview, accepted, overridden, rejected
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)

    ticket = db.relationship('Ticket', foreign_keys=[ticket_id],
                             backref=db.backref('triage_logs', lazy='dynamic'))
    actor = db.relationship('User', foreign_keys=[actor_user_id])

    def to_dict(self):
        return {
            'id': self.id,
            'ticket_id': self.ticket_id,
            'ticket_code': self.ticket_code,
            'actor_user_id': self.actor_user_id,
            'prompt_inputs': self.prompt_inputs,
            'raw_response': self.raw_response,
            'suggested': self.suggested,
            'accepted': self.accepted,
            'decision': self.decision,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f'<TicketTriageLog {self.id} ticket={self.ticket_code or self.ticket_id}>'


class AssistantPendingAction(db.Model):
    """Confirm-before-write proposals from Ask Kynvera (ticket/leave drafts)."""
    __tablename__ = 'assistant_pending_actions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    action_type = db.Column(db.String(40), nullable=False, index=True)  # create_ticket, leave_draft
    payload = db.Column(JSON, nullable=True)
    status = db.Column(db.String(20), default='pending', index=True)  # pending, confirmed, cancelled, expired
    expires_at = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)

    user = db.relationship(
        'User',
        foreign_keys=[user_id],
        backref=db.backref('assistant_pending_actions', cascade='all, delete-orphan'),
    )

    def to_public_dict(self):
        payload = self.payload or {}
        summary = payload.get('summary') if isinstance(payload, dict) else None
        if not isinstance(summary, dict):
            summary = payload if isinstance(payload, dict) else {}
        return {
            'action_id': self.id,
            'action_type': self.action_type,
            'summary': summary,
            'status': self.status,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
        }

    def __repr__(self):
        return f'<AssistantPendingAction {self.id} {self.action_type} {self.status}>'


class AssistantChatSession(db.Model):
    """One Ask Kynvera conversation. Rolls over to a new session after
    SESSION_IDLE_MINUTES of inactivity (see module_assistant/sessions.py)."""
    __tablename__ = 'assistant_chat_sessions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    title = db.Column(db.String(120), nullable=True)
    summary = db.Column(db.String(200), nullable=True)
    started_at = db.Column(db.DateTime, default=_utcnow, index=True)
    last_active_at = db.Column(db.DateTime, default=_utcnow, index=True)
    ended_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), default='active', index=True)  # active, closed

    user = db.relationship(
        'User',
        foreign_keys=[user_id],
        backref=db.backref('assistant_chat_sessions', cascade='all, delete-orphan'),
    )

    def to_public_dict(self):
        return {
            'session_id': self.id,
            'title': self.title or 'New chat',
            'summary': self.summary or '',
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'last_active_at': self.last_active_at.isoformat() if self.last_active_at else None,
            'status': self.status,
        }

    def __repr__(self):
        return f'<AssistantChatSession {self.id} user={self.user_id} {self.status}>'


class AssistantChatMessage(db.Model):
    """A single user/bot turn within an AssistantChatSession."""
    __tablename__ = 'assistant_chat_messages'

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(
        db.Integer,
        db.ForeignKey('assistant_chat_sessions.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    role = db.Column(db.String(10), nullable=False)  # user, bot
    text = db.Column(db.Text, nullable=True)
    payload = db.Column(JSON, nullable=True)  # full structured bot payload (cards/suggestions/etc)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)

    session = db.relationship(
        'AssistantChatSession',
        foreign_keys=[session_id],
        backref=db.backref(
            'messages', cascade='all, delete-orphan',
            order_by='AssistantChatMessage.created_at',
        ),
    )

    def to_public_dict(self):
        return {
            'role': self.role,
            'text': self.text,
            'payload': self.payload,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f'<AssistantChatMessage {self.id} session={self.session_id} {self.role}>'


class TicketNote(db.Model):
    """Live notes / activity on a ticket"""
    __tablename__ = 'ticket_notes'

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('tickets.id', ondelete='CASCADE'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    note_type = db.Column(db.String(30), default='note')  # note, status_change, assignment, image
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)

    author = db.relationship('User', backref=db.backref('ticket_notes', lazy='dynamic'))

    def to_dict(self):
        return {
            'id': self.id,
            'ticket_id': self.ticket_id,
            'content': self.content,
            'note_type': self.note_type,
            'author_name': self.author.full_name if self.author else 'Unknown',
            'author_id': self.user_id,
            'author_role': (
                (self.author.designation or '').replace('_', ' ').strip().title()
                if self.author and (self.author.designation or '').strip()
                else ('Admin' if self.author and (getattr(self.author, 'role', None) or '') == 'admin' else '')
            ),
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f'<TicketNote {self.id} ticket={self.ticket_id}>'


class TicketImage(db.Model):
    """Photos / images attached to a ticket"""
    __tablename__ = 'ticket_images'

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('tickets.id', ondelete='CASCADE'), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(512), nullable=False)
    cloud_url = db.Column(db.String(512), nullable=True)
    caption = db.Column(db.String(255), nullable=True)
    uploaded_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=_utcnow)

    uploader = db.relationship('User', backref=db.backref('ticket_images', lazy='dynamic'))

    def url(self):
        return self.cloud_url or f'/tickets/images/{self.id}'

    def to_dict(self):
        return {
            'id': self.id,
            'filename': self.filename,
            'url': self.url(),
            'caption': self.caption,
            'uploaded_by': self.uploader.full_name if self.uploader else None,
            'uploaded_at': self.uploaded_at.isoformat() if self.uploaded_at else None,
        }

    def __repr__(self):
        return f'<TicketImage {self.id} ticket={self.ticket_id}>'


class TicketMaterial(db.Model):
    """Materials consumed / used on a ticket (work order)"""
    __tablename__ = 'ticket_materials'

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('tickets.id', ondelete='CASCADE'), nullable=False)
    material_name = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Float, default=1.0)
    unit = db.Column(db.String(50), nullable=True)
    unit_price = db.Column(db.Float, default=0.0)
    total_price = db.Column(db.Float, default=0.0)
    from_procurement = db.Column(db.Boolean, default=False)  # sourced from procurement catalog
    procurement_ref = db.Column(db.String(80), nullable=True)  # submission_id / catalog public_id
    catalog_item_id = db.Column(db.Integer, nullable=True, index=True)
    notes = db.Column(db.Text, nullable=True)
    qty_short = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)

    def to_dict(self):
        short = float(getattr(self, 'qty_short', 0) or 0)
        qty = float(self.quantity or 0)
        created = getattr(self, 'created_at', None)
        return {
            'id': self.id,
            'material_name': self.material_name,
            'quantity': self.quantity,
            'unit': self.unit,
            'unit_price': self.unit_price,
            'total_price': self.total_price,
            'from_procurement': self.from_procurement,
            'notes': self.notes,
            'qty_short': short,
            'qty_requested': round(qty + short, 2),
            'created_at': created.isoformat() if created else None,
        }

    def __repr__(self):
        return f'<TicketMaterial {self.id} "{self.material_name}">'


class TicketManpower(db.Model):
    """Manpower hours logged on a ticket"""
    __tablename__ = 'ticket_manpower'

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('tickets.id', ondelete='CASCADE'), nullable=False)
    worker_name = db.Column(db.String(160), nullable=False)
    worker_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    hours = db.Column(db.Float, nullable=False)  # 0.25=15min, 0.5=30min, 0.75=45min, 1, 2, 3+
    rate_per_hour = db.Column(db.Float, nullable=True)
    total_cost = db.Column(db.Float, nullable=True)
    work_date = db.Column(db.Date, nullable=True)
    notes = db.Column(db.Text, nullable=True)

    worker_user = db.relationship('User', backref=db.backref('ticket_manpower_entries', lazy='dynamic'))

    def to_dict(self):
        return {
            'id': self.id,
            'worker_name': self.worker_name,
            'hours': self.hours,
            'rate_per_hour': self.rate_per_hour,
            'total_cost': self.total_cost,
            'work_date': self.work_date.isoformat() if self.work_date else None,
            'notes': self.notes,
        }

    def __repr__(self):
        return f'<TicketManpower {self.id} {self.worker_name} {self.hours}h>'


class TicketEmailIntake(db.Model):
    """Audit log of every inbound email received at the ticket intake address.

    One row per webhook call, regardless of whether it resulted in a draft ticket.
    Kept so failed/unmatched parses are visible without needing server log access.
    """
    __tablename__ = 'ticket_email_intakes'

    id = db.Column(db.Integer, primary_key=True)
    received_at = db.Column(db.DateTime, default=_utcnow, index=True)
    from_email = db.Column(db.String(255), nullable=True)
    from_name = db.Column(db.String(255), nullable=True)
    to_email = db.Column(db.String(255), nullable=True)
    subject = db.Column(db.String(500), nullable=True)
    raw_body = db.Column(db.Text, nullable=True)
    message_id = db.Column(db.String(255), nullable=True, index=True)
    # 'processed', 'duplicate', 'error', 'rejected'
    status = db.Column(db.String(20), default='processed', index=True)
    error_message = db.Column(db.Text, nullable=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('tickets.id', ondelete='SET NULL'), nullable=True)

    ticket = db.relationship('Ticket', backref=db.backref('email_intake_logs', lazy='dynamic'))

    def to_dict(self):
        return {
            'id': self.id,
            'received_at': self.received_at.isoformat() if self.received_at else None,
            'from_email': self.from_email,
            'from_name': self.from_name,
            'to_email': self.to_email,
            'subject': self.subject,
            'status': self.status,
            'error_message': self.error_message,
            'ticket_id': self.ticket_id,
        }

    def __repr__(self):
        return f'<TicketEmailIntake {self.id} from={self.from_email} status={self.status}>'


class Notification(db.Model):
    """User notifications for workflow updates"""
    __tablename__ = 'notifications'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    message = db.Column(db.Text, nullable=False)
    notification_type = db.Column(db.String(50), default='info')  # 'info', 'success', 'warning', 'error', 'hr_approved', 'hr_rejected'
    submission_id = db.Column(db.String(50), nullable=True)  # Reference to related submission
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)
    
    # Relationship
    user = db.relationship('User', backref=db.backref('notifications', lazy='dynamic', cascade='all, delete-orphan'))
    
    def to_dict(self):
        """Convert to dictionary"""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'title': self.title,
            'message': self.message,
            'notification_type': self.notification_type,
            'submission_id': self.submission_id,
            'is_read': self.is_read,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
    
    def __repr__(self):
        return f'<Notification {self.id} - User {self.user_id}>'


class Technician(db.Model):
    """Field technicians managed separately from system Users (no login required)."""
    __tablename__ = 'technicians'

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.String(40), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(160), nullable=False)
    designation = db.Column(db.String(160), nullable=True)
    department = db.Column(db.String(120), nullable=True)
    specialization = db.Column(db.String(120), nullable=True)
    phone = db.Column(db.String(40), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    salary = db.Column(db.Float, nullable=True)
    joining_date = db.Column(db.Date, nullable=True)
    status = db.Column(db.String(20), default='active', index=True)  # active, inactive, on_leave
    notes = db.Column(db.Text, nullable=True)
    # Optional link to supervisor user account (for roster reporting; ticketing team uses TicketSupervisorTeam).
    supervisor_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    supervisor_user = db.relationship(
        'User',
        foreign_keys=[supervisor_user_id],
        backref=db.backref('hr_roster_technicians', lazy='dynamic'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'employee_id': self.employee_id,
            'full_name': self.full_name,
            'designation': self.designation,
            'department': self.department,
            'specialization': self.specialization,
            'phone': self.phone,
            'email': self.email,
            'salary': self.salary,
            'joining_date': self.joining_date.isoformat() if self.joining_date else None,
            'status': self.status,
            'notes': self.notes,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'supervisor_user_id': self.supervisor_user_id,
            'supervisor_name': self.supervisor_user.full_name if self.supervisor_user else None,
            'supervisor_username': self.supervisor_user.username if self.supervisor_user else None,
        }

    def __repr__(self):
        return f'<Technician {self.employee_id} — {self.full_name}>'


class QhsiTraining(db.Model):
    """Quality team training sessions and meetings booked per project."""
    __tablename__ = 'qhsi_trainings'

    id = db.Column(db.Integer, primary_key=True)
    training_id = db.Column(db.String(40), unique=True, nullable=False, index=True)
    project_name = db.Column(db.String(255), nullable=False, index=True)
    bd_project_id = db.Column(db.Integer, db.ForeignKey('bd_projects.id'), nullable=True)
    title = db.Column(db.String(255), nullable=False)
    training_type = db.Column(db.String(30), default='training')  # training, meeting, audit, induction
    scheduled_at = db.Column(db.DateTime, nullable=False, index=True)
    duration_minutes = db.Column(db.Integer, default=60)
    location = db.Column(db.String(255))
    facilitator_name = db.Column(db.String(120))
    facilitator_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    attendees = db.Column(JSON, default=list)  # [{name, role, user_id?}]
    status = db.Column(db.String(20), default='scheduled', index=True)  # scheduled, completed, cancelled
    notes = db.Column(db.Text)
    created_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    bd_project = db.relationship('BDProject', foreign_keys=[bd_project_id])
    facilitator = db.relationship('User', foreign_keys=[facilitator_id])
    created_by = db.relationship('User', foreign_keys=[created_by_id])

    def to_dict(self):
        return {
            'id': self.id,
            'training_id': self.training_id,
            'project_name': self.project_name,
            'bd_project_id': self.bd_project_id,
            'title': self.title,
            'training_type': self.training_type,
            'scheduled_at': self.scheduled_at.isoformat() if self.scheduled_at else None,
            'duration_minutes': self.duration_minutes,
            'location': self.location,
            'facilitator_name': self.facilitator_name,
            'facilitator_id': self.facilitator_id,
            'attendees': self.attendees or [],
            'status': self.status,
            'notes': self.notes,
            'created_by_id': self.created_by_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class QhseComplianceImport(db.Model):
    """Batch metadata for Excel-imported staff compliance data."""
    __tablename__ = 'qhse_compliance_imports'

    id = db.Column(db.Integer, primary_key=True)
    import_id = db.Column(db.String(40), unique=True, nullable=False, index=True)
    filename = db.Column(db.String(255))
    row_count = db.Column(db.Integer, default=0)
    employee_count = db.Column(db.Integer, default=0)
    stats_json = db.Column(JSON, default=dict)
    imported_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)

    imported_by = db.relationship('User', foreign_keys=[imported_by_id])
    rows = db.relationship(
        'QhseStaffComplianceRow',
        back_populates='import_batch',
        cascade='all, delete-orphan',
    )

    def to_dict(self):
        return {
            'import_id': self.import_id,
            'filename': self.filename,
            'row_count': self.row_count,
            'employee_count': self.employee_count,
            'stats': self.stats_json or {},
            'imported_by_id': self.imported_by_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class QhseStaffComplianceRow(db.Model):
    """One kit line from Excel import (drives QHSE dashboard compliance metrics)."""
    __tablename__ = 'qhse_staff_compliance_rows'

    id = db.Column(db.Integer, primary_key=True)
    import_batch_id = db.Column(
        db.Integer,
        db.ForeignKey('qhse_compliance_imports.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    employee_name = db.Column(db.String(200), nullable=False, index=True)
    employee_id = db.Column(db.String(80))
    project_name = db.Column(db.String(255), nullable=False, index=True)
    record_date = db.Column(db.String(20))
    department = db.Column(db.String(120))
    supervisor_name = db.Column(db.String(120))
    notes = db.Column(db.Text)
    item_type = db.Column(db.String(80))
    item_label = db.Column(db.String(160))
    condition = db.Column(db.String(20), nullable=False, index=True)  # ok, issue, missing
    created_at = db.Column(db.DateTime, default=_utcnow)

    import_batch = db.relationship('QhseComplianceImport', back_populates='rows')

    def to_dict(self):
        return {
            'employee_name': self.employee_name,
            'employee_id': self.employee_id,
            'project_name': self.project_name,
            'record_date': self.record_date,
            'department': self.department,
            'supervisor_name': self.supervisor_name,
            'notes': self.notes,
            'item_type': self.item_type,
            'item_label': self.item_label,
            'condition': self.condition,
        }


# ── Hiring Document Tracker (HR module) ──────────────────────────────────────

# Phase 1 — collected first (candidate identity / clearance)
HIRING_PHASE1_DOC_TYPES = (
    'passport',
    'emirates_id',
    'photograph',
    'pcc',
    'education_certificate',
)

# Phase 2 — unlocked only after Phase 1 is complete
HIRING_PHASE2_DOC_TYPES = (
    'offer_letter',
    'insurance',
    'e_visa',
    'contract',
)

HIRING_DOC_TYPES = HIRING_PHASE1_DOC_TYPES + HIRING_PHASE2_DOC_TYPES

HIRING_DOC_PHASE = {
    **{dt: 1 for dt in HIRING_PHASE1_DOC_TYPES},
    **{dt: 2 for dt in HIRING_PHASE2_DOC_TYPES},
}

HIRING_DOC_LABELS = {
    'passport': 'Passport Copy (Colour)',
    'emirates_id': 'Emirates ID Copy (Colour)',
    'photograph': 'Photograph (White Background, PDF)',
    'pcc': 'PCC — Attested',
    'education_certificate': 'Education Certificate (PDF)',
    'offer_letter': 'Offer Letter (Department Signed)',
    'insurance': 'Insurance Paper',
    'e_visa': 'E-Visa',
    'contract': 'Employment Contract',
}

HIRING_DOC_ALLOWED_EXT = {
    'passport': {'pdf', 'jpg', 'jpeg', 'png'},
    'emirates_id': {'pdf', 'jpg', 'jpeg', 'png'},
    'photograph': {'pdf'},
    'pcc': {'pdf'},
    'education_certificate': {'pdf'},
    'offer_letter': {'pdf'},
    'insurance': {'pdf', 'jpg', 'jpeg', 'png'},
    'e_visa': {'pdf', 'jpg', 'jpeg', 'png'},
    'contract': {'pdf'},
}

# Docs that unlock only after pipeline reaches visa_process_started
HIRING_VISA_GATED_DOC_TYPES = frozenset({'insurance', 'e_visa', 'contract'})

# Linear hiring stages only — on_hold / not_hired are process states, not steps.
HIRING_PIPELINE_STEPS = (
    'interview_completed',
    'gathering_documents',
    'preparing_offer_letter',
    'offer_letter_prepared',
    'offer_letter_signed',
    'md_signed_offer_received',
    'gathering_documents_for_visa',
    'visa_process_started',
    'candidate_employee',
)

# Process-wide outcomes (pause or did not hire) — not linear stages.
HIRING_PIPELINE_PROCESS_STATUSES = ('on_hold', 'not_hired')

# Valid stored values = real steps + process states
HIRING_PIPELINE_STATUSES = HIRING_PIPELINE_STEPS + HIRING_PIPELINE_PROCESS_STATUSES

HIRING_PIPELINE_LABELS = {
    'interview_completed': 'Interview completed',
    'gathering_documents': 'Gathering documents',
    'preparing_offer_letter': 'Preparing offer letter',
    'offer_letter_prepared': 'Offer letter prepared',
    'offer_letter_signed': 'Offer letter signed',
    'md_signed_offer_received': 'Signed offer letter from MD received',
    'gathering_documents_for_visa': 'Gathering documents for visa process',
    'visa_process_started': 'Visa process started',
    'candidate_employee': 'Candidate employed',
    'on_hold': 'On hold',
    'not_hired': 'Not hired',
}

HIRING_PIPELINE_DEFAULT = 'interview_completed'


class HiringCandidate(db.Model):
    """Candidate / new-hire tracked for onboarding document collection."""
    __tablename__ = 'hiring_candidates'

    id = db.Column(db.Integer, primary_key=True)
    # Optional HR / Excel reference (numeric or alphanumeric). Used to match imports.
    hr_ref = db.Column(db.String(80), unique=True, nullable=True, index=True)
    full_name = db.Column(db.String(200), nullable=False, index=True)
    role = db.Column(db.String(120))  # position / job title
    department = db.Column(db.String(120))
    phone = db.Column(db.String(40))
    email = db.Column(db.String(120))
    replacement_name = db.Column(db.String(200))
    replacement_employee_id = db.Column(db.String(80))
    comments = db.Column(db.Text)
    pipeline_status = db.Column(
        db.String(40),
        default=HIRING_PIPELINE_DEFAULT,
        index=True,
    )
    leave_employee_id = db.Column(
        db.Integer,
        db.ForeignKey('leave_employees.id', ondelete='SET NULL'),
        unique=True,
        nullable=True,
        index=True,
    )
    employee_list_dismissed_at = db.Column(db.DateTime, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, index=True)

    creator = db.relationship('User', foreign_keys=[created_by],
                              backref=db.backref('hiring_candidates_created', lazy='dynamic'))
    documents = db.relationship(
        'HiringDocument',
        back_populates='candidate',
        cascade='all, delete-orphan',
        lazy='joined',
    )
    assigned_vacancy = db.relationship(
        'ManpowerVacancy',
        back_populates='hiring_candidate',
        uselist=False,
        lazy='select',
    )
    leave_employee = db.relationship(
        'LeaveEmployee',
        foreign_keys=[leave_employee_id],
        uselist=False,
        lazy='select',
    )

    @staticmethod
    def doc_is_complete(doc) -> bool:
        """Whether a document counts as done for progress."""
        if not doc:
            return False
        if doc.doc_type == 'pcc':
            return doc.status in ('attested', 'verified')
        return doc.status in ('uploaded', 'attested', 'verified')

    def _docs_by_type(self):
        return {d.doc_type: d for d in (self.documents or [])}

    def phase_progress(self, doc_types):
        by_type = self._docs_by_type()
        total = len(doc_types)
        completed = sum(1 for dt in doc_types if self.doc_is_complete(by_type.get(dt)))
        return completed, total

    def phase1_complete(self) -> bool:
        completed, total = self.phase_progress(HIRING_PHASE1_DOC_TYPES)
        return completed >= total

    def normalized_pipeline_status(self) -> str:
        status = (self.pipeline_status or HIRING_PIPELINE_DEFAULT).strip()
        if status not in HIRING_PIPELINE_STATUSES:
            return HIRING_PIPELINE_DEFAULT
        return status

    def is_on_hold(self) -> bool:
        """True when the whole hiring process is paused (not a linear stage)."""
        return self.normalized_pipeline_status() == 'on_hold'

    def is_not_hired(self) -> bool:
        """True when the candidate was not hired (process closed, not a linear stage)."""
        return self.normalized_pipeline_status() == 'not_hired'

    def pipeline_index(self) -> int:
        """Index within HIRING_PIPELINE_STEPS; -1 for process states (on hold / not hired)."""
        status = self.normalized_pipeline_status()
        try:
            return HIRING_PIPELINE_STEPS.index(status)
        except ValueError:
            return -1

    def visa_docs_unlocked(self) -> bool:
        """Insurance, e-visa, and contract unlock at visa_process_started (not while off-stage)."""
        if self.pipeline_index() < 0:
            return False
        visa_idx = HIRING_PIPELINE_STEPS.index('visa_process_started')
        return self.pipeline_index() >= visa_idx

    def file_closed(self) -> bool:
        """True when hiring file is closed (candidate employee final stage)."""
        return self.normalized_pipeline_status() == 'candidate_employee'

    def pipeline_steps(self):
        """Linear stage chips only — excludes on_hold / not_hired (process states)."""
        current = self.normalized_pipeline_status()
        current_idx = self.pipeline_index()
        frozen = current_idx < 0
        steps = []
        for i, key in enumerate(HIRING_PIPELINE_STEPS):
            steps.append({
                'key': key,
                'label': HIRING_PIPELINE_LABELS.get(key, key),
                'done': (not frozen) and current_idx > i,
                'current': (not frozen) and key == current,
            })
        return steps

    def progress(self):
        """Overall progress across both phases (all 9 documents)."""
        p1_done, p1_total = self.phase_progress(HIRING_PHASE1_DOC_TYPES)
        p2_done, p2_total = self.phase_progress(HIRING_PHASE2_DOC_TYPES)
        completed = p1_done + p2_done
        total = p1_total + p2_total

        if completed <= 0:
            status = 'not_started'
        elif completed >= total:
            status = 'complete'
        else:
            status = 'in_progress'
        return completed, total, status

    def initials(self) -> str:
        parts = (self.full_name or '').strip().split()
        if not parts:
            return '?'
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    def _placeholder_doc(self, dt):
        return {
            'id': None,
            'candidate_id': self.id,
            'doc_type': dt,
            'label': HIRING_DOC_LABELS.get(dt, dt),
            'phase': HIRING_DOC_PHASE.get(dt, 1),
            'status': 'missing',
            'filename': None,
            'mime_type': None,
            'file_size': None,
            'uploaded_at': None,
            'uploaded_by': None,
            'has_file': False,
            'is_complete': False,
            'file_url': None,
            'notes': '',
            'allowed_extensions': sorted(HIRING_DOC_ALLOWED_EXT.get(dt, set())),
        }

    def to_dict(self, include_documents=True):
        completed, total, status = self.progress()
        p1_done, p1_total = self.phase_progress(HIRING_PHASE1_DOC_TYPES)
        p2_done, p2_total = self.phase_progress(HIRING_PHASE2_DOC_TYPES)
        pipeline = self.normalized_pipeline_status()
        visa_unlocked = self.visa_docs_unlocked()
        d = {
            'id': self.id,
            'hr_ref': (self.hr_ref or '').strip() or str(self.id),
            'full_name': self.full_name,
            'role': self.role or '',
            'department': self.department or '',
            'phone': self.phone or '',
            'email': self.email or '',
            'replacement_name': self.replacement_name or '',
            'replacement_employee_id': self.replacement_employee_id or '',
            'comments': self.comments or '',
            'initials': self.initials(),
            'completed': completed,
            'total': total,
            'progress_label': f'{completed}/{total}',
            'status': status,
            'pipeline_status': pipeline,
            'pipeline_label': HIRING_PIPELINE_LABELS.get(pipeline, pipeline),
            'pipeline_steps': self.pipeline_steps(),
            'is_on_hold': pipeline == 'on_hold',
            'is_not_hired': pipeline == 'not_hired',
            'file_closed': pipeline == 'candidate_employee',
            'leave_employee_id': self.leave_employee_id,
            'on_employee_list': bool(
                self.leave_employee_id
                and self.leave_employee
                and getattr(self.leave_employee, 'active', False)
            ),
            'visa_docs_unlocked': visa_unlocked,
            'phase1_completed': p1_done,
            'phase1_total': p1_total,
            'phase2_completed': p2_done,
            'phase2_total': p2_total,
            'phase2_unlocked': True,
            'created_by': self.created_by,
            'created_at': naive_utc_isoformat_z(self.created_at) if self.created_at else None,
            'updated_at': naive_utc_isoformat_z(self.updated_at) if self.updated_at else None,
            'vacancy_id': None,
            'vacancy': None,
        }
        vac = None
        try:
            vac = self.assigned_vacancy
        except Exception:
            vac = None
        if vac is not None:
            trade = vac.trade
            project = vac.project
            req = vac.normalized_requirement_type()
            d['vacancy_id'] = vac.id
            d['vacancy'] = {
                'id': vac.id,
                'trade_id': vac.trade_id,
                'trade_name': trade.name if trade else None,
                'project_id': vac.project_id,
                'project_name': project.name if project else None,
                'requirement_type': req,
                'requirement_type_label': MANPOWER_REQUIREMENT_TYPE_LABELS.get(req, req),
                'replacement_name': vac.replacement_name or '',
                'replacement_employee_id': vac.replacement_employee_id or '',
                'status': vac.normalized_status(),
                'status_label': MANPOWER_STATUS_LABELS.get(
                    vac.normalized_status(), vac.normalized_status()
                ),
                'label': ' - '.join(
                    x for x in [
                        trade.name if trade else None,
                        project.name if project else None,
                    ] if x
                ),
            }
        if include_documents:
            by_type = self._docs_by_type()
            docs = []
            for dt in HIRING_DOC_TYPES:
                doc = by_type.get(dt)
                if doc:
                    item = doc.to_dict()
                else:
                    item = self._placeholder_doc(dt)
                item['upload_locked'] = (
                    dt in HIRING_VISA_GATED_DOC_TYPES and not visa_unlocked
                )
                docs.append(item)
            d['documents'] = docs
            d['phase1_documents'] = [x for x in docs if x.get('phase') == 1]
            d['phase2_documents'] = [x for x in docs if x.get('phase') == 2]
        letters = []
        if include_documents and self.id:
            try:
                letters = [
                    x.to_dict(include_candidate=False)
                    for x in (self.linked_offer_letters or [])
                ]
            except Exception:
                letters = []
        d['linked_offer_letters'] = letters
        return d

    def __repr__(self):
        return f'<HiringCandidate {self.id} {self.full_name}>'


class HiringDocument(db.Model):
    """One onboarding document slot for a hiring candidate."""
    __tablename__ = 'hiring_documents'

    id = db.Column(db.Integer, primary_key=True)
    candidate_id = db.Column(
        db.Integer,
        db.ForeignKey('hiring_candidates.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    doc_type = db.Column(db.String(40), nullable=False, index=True)
    filename = db.Column(db.String(255))
    file_path = db.Column(db.String(500))
    cloud_url = db.Column(db.String(500))
    mime_type = db.Column(db.String(100))
    file_size = db.Column(db.Integer)
    status = db.Column(db.String(20), default='missing', index=True)  # missing|uploaded|attested|verified
    notes = db.Column(db.Text)  # optional per-doc note (UI currently for offer letter)
    uploaded_at = db.Column(db.DateTime)
    uploaded_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    __table_args__ = (
        db.UniqueConstraint('candidate_id', 'doc_type', name='uq_hiring_candidate_doc_type'),
    )

    candidate = db.relationship('HiringCandidate', back_populates='documents')
    uploader = db.relationship('User', foreign_keys=[uploaded_by],
                               backref=db.backref('hiring_documents_uploaded', lazy='dynamic'))

    def has_file(self) -> bool:
        return bool(self.cloud_url or self.file_path)

    def file_url(self):
        if self.id and self.has_file():
            return f'/hr/api/hiring/documents/{self.id}/file'
        return None

    def to_dict(self):
        return {
            'id': self.id,
            'candidate_id': self.candidate_id,
            'doc_type': self.doc_type,
            'label': HIRING_DOC_LABELS.get(self.doc_type, self.doc_type),
            'phase': HIRING_DOC_PHASE.get(self.doc_type, 1),
            'filename': self.filename,
            'mime_type': self.mime_type,
            'file_size': self.file_size,
            'status': self.status or 'missing',
            'uploaded_at': self.uploaded_at.isoformat() if self.uploaded_at else None,
            'uploaded_by': self.uploaded_by,
            'uploader_name': (
                self.uploader.full_name or self.uploader.username
                if self.uploader else None
            ),
            'has_file': self.has_file(),
            'is_complete': HiringCandidate.doc_is_complete(self),
            'file_url': self.file_url(),
            'notes': self.notes or '',
            'allowed_extensions': sorted(HIRING_DOC_ALLOWED_EXT.get(self.doc_type, set())),
        }

    def __repr__(self):
        return f'<HiringDocument {self.id} {self.doc_type} cand={self.candidate_id}>'


# ── Offer Letters / Letter of Intent register (Hiring Docs sub-module) ──────

HIRING_OFFER_LETTER_KINDS = ('offer_letter', 'letter_of_intent')

HIRING_OFFER_LETTER_KIND_LABELS = {
    'offer_letter': 'Offer Letter',
    'letter_of_intent': 'Letter of Intent',
}

HIRING_OFFER_LETTER_LINK_STATUSES = ('unlinked', 'linked', 'manual')

HIRING_OFFER_LETTER_ALLOWED_EXT = {'pdf', 'jpg', 'jpeg', 'png'}


class HiringOfferLetter(db.Model):
    """Inbox for scanned offer letters / letters of intent, optionally linked to hiring."""
    __tablename__ = 'hiring_offer_letters'

    id = db.Column(db.Integer, primary_key=True)
    doc_kind = db.Column(db.String(40), nullable=False, default='letter_of_intent', index=True)
    full_name = db.Column(db.String(200), nullable=False, index=True)
    role = db.Column(db.String(120))
    department = db.Column(db.String(120))
    phone = db.Column(db.String(40))
    email = db.Column(db.String(120))
    comments = db.Column(db.Text)

    received = db.Column(db.Boolean, default=False, nullable=False, index=True)
    signed_back = db.Column(db.Boolean, default=False, nullable=False, index=True)
    # Step 2 outcome after HR scan: False = awaiting signature; True = offer declined
    not_accepted = db.Column(db.Boolean, default=False, nullable=False, index=True)

    filename = db.Column(db.String(255))
    file_path = db.Column(db.String(500))
    cloud_url = db.Column(db.String(500))
    mime_type = db.Column(db.String(100))
    file_size = db.Column(db.Integer)

    signed_filename = db.Column(db.String(255))
    signed_file_path = db.Column(db.String(500))
    signed_cloud_url = db.Column(db.String(500))
    signed_mime_type = db.Column(db.String(100))
    signed_file_size = db.Column(db.Integer)

    hiring_candidate_id = db.Column(
        db.Integer,
        db.ForeignKey('hiring_candidates.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
    )
    link_status = db.Column(db.String(20), default='unlinked', nullable=False, index=True)

    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, index=True)

    hiring_candidate = db.relationship(
        'HiringCandidate',
        backref=db.backref('linked_offer_letters', lazy='select'),
        foreign_keys=[hiring_candidate_id],
    )
    creator = db.relationship(
        'User',
        foreign_keys=[created_by],
        backref=db.backref('hiring_offer_letters_created', lazy='dynamic'),
    )

    def normalized_kind(self) -> str:
        kind = (self.doc_kind or 'letter_of_intent').strip()
        if kind not in HIRING_OFFER_LETTER_KINDS:
            return 'letter_of_intent'
        return kind

    def normalized_link_status(self) -> str:
        status = (self.link_status or 'unlinked').strip()
        if self.hiring_candidate_id and status != 'linked':
            return 'linked'
        if status not in HIRING_OFFER_LETTER_LINK_STATUSES:
            return 'unlinked'
        return status

    def initials(self) -> str:
        parts = (self.full_name or '').strip().split()
        if not parts:
            return '?'
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    def has_scan_file(self) -> bool:
        return bool(self.cloud_url or self.file_path)

    def has_signed_file(self) -> bool:
        return bool(self.signed_cloud_url or self.signed_file_path)

    def scan_file_url(self):
        if self.id and self.has_scan_file():
            return f'/hr/api/hiring/offer-letters/{self.id}/file?kind=scan'
        return None

    def signed_file_url(self):
        if self.id and self.has_signed_file():
            return f'/hr/api/hiring/offer-letters/{self.id}/file?kind=signed'
        return None

    def best_file_kind(self):
        if self.has_signed_file():
            return 'signed'
        if self.has_scan_file():
            return 'scan'
        return None

    def prompt_connect(self) -> bool:
        return bool(self.received) and self.normalized_link_status() == 'unlinked'

    def candidate_outcome(self) -> str:
        """Step-2 status after the unsigned HR letter is on file."""
        if not self.received:
            return 'pending_hr'
        if self.not_accepted:
            return 'not_accepted'
        if self.signed_back:
            return 'signed'
        return 'awaiting_signature'

    def to_dict(self, include_candidate=True):
        kind = self.normalized_kind()
        link = self.normalized_link_status()
        outcome = self.candidate_outcome()
        d = {
            'id': self.id,
            'doc_kind': kind,
            'doc_kind_label': HIRING_OFFER_LETTER_KIND_LABELS.get(kind, kind),
            'full_name': self.full_name,
            'role': self.role or '',
            'department': self.department or '',
            'phone': self.phone or '',
            'email': self.email or '',
            'comments': self.comments or '',
            'initials': self.initials(),
            'received': bool(self.received),
            'signed_back': bool(self.signed_back),
            'not_accepted': bool(self.not_accepted),
            'candidate_outcome': outcome,
            'has_scan_file': self.has_scan_file(),
            'has_signed_file': self.has_signed_file(),
            'filename': self.filename,
            'signed_filename': self.signed_filename,
            'mime_type': self.mime_type,
            'signed_mime_type': self.signed_mime_type,
            'file_size': self.file_size,
            'signed_file_size': self.signed_file_size,
            'scan_file_url': self.scan_file_url(),
            'signed_file_url': self.signed_file_url(),
            'hiring_candidate_id': self.hiring_candidate_id,
            'link_status': link,
            'prompt_connect': self.prompt_connect(),
            'allowed_extensions': sorted(HIRING_OFFER_LETTER_ALLOWED_EXT),
            'created_by': self.created_by,
            'created_at': naive_utc_isoformat_z(self.created_at) if self.created_at else None,
            'updated_at': naive_utc_isoformat_z(self.updated_at) if self.updated_at else None,
            'hiring_candidate': None,
        }
        if include_candidate and self.hiring_candidate is not None:
            cand = self.hiring_candidate
            d['hiring_candidate'] = {
                'id': cand.id,
                'full_name': cand.full_name,
                'role': cand.role or '',
                'pipeline_status': cand.normalized_pipeline_status(),
                'pipeline_label': HIRING_PIPELINE_LABELS.get(
                    cand.normalized_pipeline_status(), cand.normalized_pipeline_status()
                ),
            }
        return d

    def __repr__(self):
        return f'<HiringOfferLetter {self.id} {self.doc_kind} {self.full_name}>'


# ── Leave Tracker (Sick + Annual from Jan 2026) ─────────────────────────────

LEAVE_TRACKER_YEAR = 2026
LEAVE_TRACKER_MONTHS = (7, 8, 9, 10, 11, 12)  # default month cards
LEAVE_ALL_MONTHS = tuple(range(1, 13))
LEAVE_WINDOW_START = date(LEAVE_TRACKER_YEAR, 1, 1)
LEAVE_WINDOW_END = date(2035, 12, 31)
LEAVE_TRACKER_MONTH_LABELS = {
    1: 'Jan',
    2: 'Feb',
    3: 'Mar',
    4: 'Apr',
    5: 'May',
    6: 'Jun',
    7: 'Jul',
    8: 'Aug',
    9: 'Sep',
    10: 'Oct',
    11: 'Nov',
    12: 'Dec',
}


def leave_months_through(month: int) -> tuple:
    """Calendar months from January through ``month`` (inclusive)."""
    return tuple(m for m in LEAVE_ALL_MONTHS if m <= month)


def leave_months_before(month: int) -> tuple:
    """Calendar months before ``month`` (empty for January)."""
    return tuple(m for m in LEAVE_ALL_MONTHS if m < month)


LEAVE_SICK_ENTITLEMENT = 15
LEAVE_TYPES = ('sick', 'annual')
LEAVE_COMPANIES = ('Kynvera', 'Tourism', 'L&P')


def normalize_leave_company(value, default='Kynvera'):
    """Map stored/imported company labels onto LEAVE_COMPANIES (legacy INJAAZ → Kynvera)."""
    raw = (value or '').strip()
    if not raw:
        return default
    key = raw.upper().replace(' LLC', '').strip()
    if key in ('INJAAZ', 'KYNVERA'):
        return 'Kynvera'
    for company in LEAVE_COMPANIES:
        if company.lower() == raw.lower():
            return company
    return None


def parse_employee_company(value, default='Kynvera'):
    """Canonicalize known companies; keep any other typed label (fits LeaveEmployee.company)."""
    known = normalize_leave_company(value, default=None)
    if known:
        return known
    raw = (value or '').strip()
    if not raw:
        return default
    return raw[:40]


def leave_company_db_values(company):
    """DB values that count as this UI company, including legacy INJAAZ rows."""
    norm = normalize_leave_company(company, default=None)
    if norm == 'Kynvera':
        return ('Kynvera', 'INJAAZ')
    if norm:
        return (norm,)
    return (company,) if company else ('Kynvera',)

# Sick-used thresholds for UI / Excel highlighting
LEAVE_SICK_ALERT_WARNING = 10   # approaching
LEAVE_SICK_ALERT_CRITICAL = 13  # nearly exhausted
# exhausted = LEAVE_SICK_ENTITLEMENT (15+)


def leave_sick_alert_level(used: float) -> str:
    """Return '' | 'warning' | 'critical' | 'exhausted' from sick days used."""
    if used is None:
        return ''
    try:
        u = float(used)
    except (TypeError, ValueError):
        return ''
    if u >= LEAVE_SICK_ENTITLEMENT:
        return 'exhausted'
    if u >= LEAVE_SICK_ALERT_CRITICAL:
        return 'critical'
    if u >= LEAVE_SICK_ALERT_WARNING:
        return 'warning'
    return ''


class LeaveEmployee(db.Model):
    """Staff roster row for the HR leave tracker (may include EMP IDs not in users)."""
    __tablename__ = 'leave_employees'

    id = db.Column(db.Integer, primary_key=True)
    emp_id = db.Column(db.String(40), nullable=False, unique=True, index=True)
    full_name = db.Column(db.String(200), nullable=False, index=True)
    designation = db.Column(db.String(160))
    company = db.Column(db.String(40), nullable=False, default='Kynvera', index=True)
    annual_entitlement = db.Column(db.Integer, nullable=True)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    usage = db.relationship(
        'LeaveMonthlyUsage',
        back_populates='employee',
        cascade='all, delete-orphan',
        lazy='select',
    )
    plans = db.relationship(
        'LeavePlan',
        back_populates='employee',
        cascade='all, delete-orphan',
        lazy='select',
    )
    logs = db.relationship(
        'LeaveLog',
        back_populates='employee',
        cascade='all, delete-orphan',
        lazy='select',
    )

    def month_days(self, leave_type: str, year: int, month: int):
        """Return days for a month, or None if not yet entered."""
        for u in self.usage or []:
            if u.leave_type == leave_type and u.year == year and u.month == month:
                return u.days
        return None

    def used_total(self, leave_type: str, year: int = LEAVE_TRACKER_YEAR, months=None) -> float:
        months = months if months is not None else LEAVE_ALL_MONTHS
        total = 0.0
        for m in months:
            d = self.month_days(leave_type, year, m)
            if d is not None:
                try:
                    total += float(d)
                except (TypeError, ValueError):
                    pass
        return total

    def usage_map(self, leave_type: str, year: int = LEAVE_TRACKER_YEAR, months=None):
        """Dict month -> days (None if empty) for the requested months."""
        months = months if months is not None else tuple(range(1, 13))
        return {m: self.month_days(leave_type, year, m) for m in months}

    def to_dict(self, year: int = LEAVE_TRACKER_YEAR):
        sick_map = self.usage_map('sick', year)
        annual_map = self.usage_map('annual', year)
        sick_used = self.used_total('sick', year)
        annual_used = self.used_total('annual', year)
        entitlement = self.annual_entitlement
        annual_remaining = None
        if entitlement is not None:
            annual_remaining = float(entitlement) - annual_used
        alert = leave_sick_alert_level(sick_used)
        return {
            'id': self.id,
            'emp_id': self.emp_id,
            'full_name': self.full_name,
            'designation': self.designation or '',
            'company': normalize_leave_company(self.company) or self.company or '',
            'annual_entitlement': entitlement,
            'active': bool(self.active),
            'sick': {
                'months': sick_map,
                'used': sick_used,
                'remaining': LEAVE_SICK_ENTITLEMENT - sick_used,
                'entitlement': LEAVE_SICK_ENTITLEMENT,
                'alert': alert,
            },
            'annual': {
                'months': annual_map,
                'used': annual_used,
                'remaining': annual_remaining,
                'entitlement': entitlement,
            },
            'created_at': naive_utc_isoformat_z(self.created_at) if self.created_at else None,
            'updated_at': naive_utc_isoformat_z(self.updated_at) if self.updated_at else None,
        }

    def __repr__(self):
        return f'<LeaveEmployee {self.id} {self.emp_id} {self.full_name}>'


class LeaveMonthlyUsage(db.Model):
    """Days taken in a given year-month for sick or annual leave (rolled-up cache from leave_logs)."""
    __tablename__ = 'leave_monthly_usage'

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(
        db.Integer,
        db.ForeignKey('leave_employees.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    leave_type = db.Column(db.String(20), nullable=False, index=True)  # sick | annual
    year = db.Column(db.Integer, nullable=False, index=True)
    month = db.Column(db.Integer, nullable=False)  # 1–12
    days = db.Column(db.Float, nullable=True)  # None = not yet entered
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    employee = db.relationship('LeaveEmployee', back_populates='usage')

    __table_args__ = (
        db.UniqueConstraint(
            'employee_id', 'leave_type', 'year', 'month',
            name='uq_leave_monthly_emp_type_ym',
        ),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'employee_id': self.employee_id,
            'leave_type': self.leave_type,
            'year': self.year,
            'month': self.month,
            'days': self.days,
            'updated_at': naive_utc_isoformat_z(self.updated_at) if self.updated_at else None,
        }

    def __repr__(self):
        return (
            f'<LeaveMonthlyUsage emp={self.employee_id} '
            f'{self.leave_type} {self.year}-{self.month} days={self.days}>'
        )


class LeaveLog(db.Model):
    """Individual leave event — source of truth; rolls up into leave_monthly_usage."""
    __tablename__ = 'leave_logs'

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(
        db.Integer,
        db.ForeignKey('leave_employees.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    leave_type = db.Column(db.String(20), nullable=False, index=True)  # sick | annual
    leave_date = db.Column(db.Date, nullable=False, index=True)  # start date
    end_date = db.Column(db.Date, nullable=True)  # inclusive end; None = same as leave_date
    days = db.Column(db.Float, nullable=False)
    notes = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    employee = db.relationship('LeaveEmployee', back_populates='logs')
    creator = db.relationship('User', foreign_keys=[created_by])

    def effective_end(self):
        return self.end_date or self.leave_date

    def to_dict(self):
        emp = self.employee
        month = self.leave_date.month if self.leave_date else None
        end = self.effective_end()
        return {
            'id': self.id,
            'employee_id': self.employee_id,
            'emp_id': emp.emp_id if emp else None,
            'full_name': emp.full_name if emp else None,
            'company': emp.company if emp else None,
            'designation': (emp.designation or '') if emp else '',
            'leave_type': self.leave_type,
            'leave_date': self.leave_date.isoformat() if self.leave_date else None,
            'end_date': end.isoformat() if end else None,
            'year': self.leave_date.year if self.leave_date else None,
            'month': month,
            'month_label': LEAVE_TRACKER_MONTH_LABELS.get(month, '') if month else '',
            'days': self.days,
            'notes': self.notes or '',
            'created_by': self.created_by,
            'created_at': naive_utc_isoformat_z(self.created_at) if self.created_at else None,
            'updated_at': naive_utc_isoformat_z(self.updated_at) if self.updated_at else None,
        }

    def __repr__(self):
        return (
            f'<LeaveLog {self.id} emp={self.employee_id} '
            f'{self.leave_type} {self.leave_date} days={self.days}>'
        )


def _log_days_in_month(log: 'LeaveLog', year: int, month: int) -> float:
    """How many calendar days of this log fall in year-month."""
    from datetime import timedelta

    start = log.leave_date
    end = log.end_date or log.leave_date
    if not start or not end or end < start:
        return 0.0
    # Prefer proportional split of stored days if single-day vs multi
    # Count calendar days in range that land in this month, then scale if days != calendar span
    cal_total = (end - start).days + 1
    if cal_total <= 0:
        return 0.0
    count = 0
    cur = start
    while cur <= end:
        if cur.year == year and cur.month == month:
            count += 1
        cur += timedelta(days=1)
    if count <= 0:
        return 0.0
    stored = float(log.days or 0)
    if abs(stored - cal_total) < 0.01:
        return float(count)
    # Partial days / custom days: distribute proportionally
    return stored * (count / cal_total)


def recompute_monthly_usage(employee_id: int, leave_type: str, year: int, month: int) -> float:
    """
    Sum leave_logs (including multi-day ranges) into leave_monthly_usage for one month.
    Deletes the usage row when the sum is 0. Returns the new total.
    """
    logs = LeaveLog.query.filter_by(
        employee_id=employee_id,
        leave_type=leave_type,
    ).all()
    total = 0.0
    for lg in logs:
        total += _log_days_in_month(lg, year, month)
    total = round(total, 2)

    row = LeaveMonthlyUsage.query.filter_by(
        employee_id=employee_id,
        leave_type=leave_type,
        year=year,
        month=month,
    ).first()

    if total <= 0:
        if row:
            db.session.delete(row)
        return 0.0

    if not row:
        row = LeaveMonthlyUsage(
            employee_id=employee_id,
            leave_type=leave_type,
            year=year,
            month=month,
        )
        db.session.add(row)
    row.days = total
    row.updated_at = _utcnow()
    return total


def months_touched_by_range(start, end):
    """Yield (year, month) pairs overlapped by inclusive date range."""
    from datetime import timedelta
    if not start or not end or end < start:
        return []
    seen = []
    cur = start
    while cur <= end:
        key = (cur.year, cur.month)
        if not seen or seen[-1] != key:
            seen.append(key)
        cur += timedelta(days=1)
    return seen


def migrate_monthly_usage_to_logs() -> dict:
    """
    One-time: if leave_logs is empty but leave_monthly_usage has rows,
    create synthetic logs dated 1st of each month so history is preserved.
    """
    if LeaveLog.query.count() > 0:
        return {'migrated': 0, 'skipped': True, 'reason': 'logs_already_exist'}

    rows = LeaveMonthlyUsage.query.filter(
        LeaveMonthlyUsage.days.isnot(None),
        LeaveMonthlyUsage.days > 0,
    ).all()
    created = 0
    for u in rows:
        try:
            leave_date = date(u.year, u.month, 1)
        except ValueError:
            continue
        db.session.add(LeaveLog(
            employee_id=u.employee_id,
            leave_type=u.leave_type,
            leave_date=leave_date,
            days=float(u.days),
            notes='Migrated from monthly total',
        ))
        created += 1
    if created:
        db.session.commit()
    return {'migrated': created, 'skipped': False}


class LeavePlan(db.Model):
    """Planned annual leave date range for an employee."""
    __tablename__ = 'leave_plans'

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(
        db.Integer,
        db.ForeignKey('leave_employees.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    days = db.Column(db.Integer, nullable=False)  # inclusive calendar days
    notes = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    employee = db.relationship('LeaveEmployee', back_populates='plans')
    creator = db.relationship('User', foreign_keys=[created_by])

    @staticmethod
    def calendar_days(start, end) -> int:
        if not start or not end or end < start:
            return 0
        return (end - start).days + 1

    def to_dict(self):
        emp = self.employee
        return {
            'id': self.id,
            'employee_id': self.employee_id,
            'emp_id': emp.emp_id if emp else None,
            'full_name': emp.full_name if emp else None,
            'company': emp.company if emp else None,
            'start_date': self.start_date.isoformat() if self.start_date else None,
            'end_date': self.end_date.isoformat() if self.end_date else None,
            'days': self.days,
            'notes': self.notes or '',
            'created_by': self.created_by,
            'created_at': naive_utc_isoformat_z(self.created_at) if self.created_at else None,
            'updated_at': naive_utc_isoformat_z(self.updated_at) if self.updated_at else None,
        }

    def __repr__(self):
        return f'<LeavePlan {self.id} emp={self.employee_id} {self.start_date}–{self.end_date}>'


# ── Manpower / vacancy tracker (HR) ─────────────────────────────────────────

MANPOWER_STATUSES = (
    'open',
    'interviewing',
    'selected',
    'filled',
    'joined',
    'on_hold',
)

MANPOWER_STATUS_LABELS = {
    'open': 'Open',
    'interviewing': 'Interviewing',
    'selected': 'Selected',
    'filled': 'Filled',
    'joined': 'Joined',
    'on_hold': 'On Hold',
}

MANPOWER_STATUS_DEFAULT = 'open'

# “In Progress” on the dashboard = interviewing + selected + on_hold
MANPOWER_IN_PROGRESS_STATUSES = frozenset({'interviewing', 'selected', 'on_hold'})

MANPOWER_REQUIREMENT_TYPES = ('new', 'replacement')

MANPOWER_REQUIREMENT_TYPE_LABELS = {
    'new': 'New',
    'replacement': 'Replacement',
}

MANPOWER_REQUIREMENT_TYPE_DEFAULT = 'new'


class ManpowerTrade(db.Model):
    """Trade / designation list for the manpower vacancy board."""
    __tablename__ = 'manpower_trades'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True, index=True)
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    vacancies = db.relationship(
        'ManpowerVacancy',
        back_populates='trade',
        lazy='dynamic',
    )

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'sort_order': self.sort_order or 0,
            'active': bool(self.active),
        }

    def __repr__(self):
        return f'<ManpowerTrade {self.id} {self.name}>'


class ManpowerProject(db.Model):
    """Project list for the manpower vacancy board (standalone; TicketProject link later)."""
    __tablename__ = 'manpower_projects'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, unique=True, index=True)
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    vacancies = db.relationship(
        'ManpowerVacancy',
        back_populates='project',
        lazy='dynamic',
    )

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'sort_order': self.sort_order or 0,
            'active': bool(self.active),
        }

    def __repr__(self):
        return f'<ManpowerProject {self.id} {self.name}>'


class ManpowerVacancy(db.Model):
    """One required headcount slot (Excel All Trades row)."""
    __tablename__ = 'manpower_vacancies'

    id = db.Column(db.Integer, primary_key=True)
    trade_id = db.Column(
        db.Integer,
        db.ForeignKey('manpower_trades.id', ondelete='RESTRICT'),
        nullable=False,
        index=True,
    )
    project_id = db.Column(
        db.Integer,
        db.ForeignKey('manpower_projects.id', ondelete='RESTRICT'),
        nullable=False,
        index=True,
    )
    requirement_type = db.Column(
        db.String(20),
        default=MANPOWER_REQUIREMENT_TYPE_DEFAULT,
        nullable=False,
        index=True,
    )
    replacement_name = db.Column(db.String(200))
    replacement_employee_id = db.Column(db.String(80))
    candidate_name = db.Column(db.String(200))
    contact_number = db.Column(db.String(60))
    status = db.Column(
        db.String(20),
        default=MANPOWER_STATUS_DEFAULT,
        nullable=False,
        index=True,
    )
    date_joined = db.Column(db.Date, nullable=True)
    remarks = db.Column(db.Text)
    sort_order = db.Column(db.Integer, default=0, nullable=False)
    hiring_candidate_id = db.Column(
        db.Integer,
        db.ForeignKey('hiring_candidates.id', ondelete='SET NULL'),
        nullable=True,
        unique=True,
        index=True,
    )
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, index=True)

    trade = db.relationship('ManpowerTrade', back_populates='vacancies')
    project = db.relationship('ManpowerProject', back_populates='vacancies')
    hiring_candidate = db.relationship(
        'HiringCandidate',
        back_populates='assigned_vacancy',
        foreign_keys=[hiring_candidate_id],
    )
    creator = db.relationship('User', foreign_keys=[created_by])

    def normalized_status(self) -> str:
        s = (self.status or MANPOWER_STATUS_DEFAULT).strip().lower()
        if s not in MANPOWER_STATUSES:
            return MANPOWER_STATUS_DEFAULT
        return s

    def normalized_requirement_type(self) -> str:
        t = (self.requirement_type or MANPOWER_REQUIREMENT_TYPE_DEFAULT).strip().lower()
        if t not in MANPOWER_REQUIREMENT_TYPES:
            return MANPOWER_REQUIREMENT_TYPE_DEFAULT
        return t

    def to_dict(self, *, person_of=None, person_total=None):
        trade = self.trade
        project = self.project
        status = self.normalized_status()
        req = self.normalized_requirement_type()
        d = {
            'id': self.id,
            'trade_id': self.trade_id,
            'trade_name': trade.name if trade else None,
            'project_id': self.project_id,
            'project_name': project.name if project else None,
            'requirement_type': req,
            'requirement_type_label': MANPOWER_REQUIREMENT_TYPE_LABELS.get(req, req),
            'replacement_name': self.replacement_name or '',
            'replacement_employee_id': self.replacement_employee_id or '',
            'candidate_name': self.candidate_name or '',
            'contact_number': self.contact_number or '',
            'status': status,
            'status_label': MANPOWER_STATUS_LABELS.get(status, status),
            'date_joined': self.date_joined.isoformat() if self.date_joined else None,
            'remarks': self.remarks or '',
            'sort_order': self.sort_order or 0,
            'hiring_candidate_id': self.hiring_candidate_id,
            'linked': bool(self.hiring_candidate_id),
            'hiring_url': (
                f'/hr/hiring/candidates/{self.hiring_candidate_id}'
                if self.hiring_candidate_id else None
            ),
            'created_by': self.created_by,
            'created_at': naive_utc_isoformat_z(self.created_at) if self.created_at else None,
            'updated_at': naive_utc_isoformat_z(self.updated_at) if self.updated_at else None,
        }
        cand = None
        try:
            cand = self.hiring_candidate
        except Exception:
            cand = None
        if cand is not None:
            try:
                completed, total, progress_status = cand.progress()
                pipeline = cand.normalized_pipeline_status()
                d['hiring_candidate'] = {
                    'id': cand.id,
                    'full_name': cand.full_name,
                    'role': cand.role or '',
                    'phone': cand.phone or '',
                    'pipeline_status': pipeline,
                    'pipeline_label': HIRING_PIPELINE_LABELS.get(pipeline, pipeline),
                    'progress_label': f'{completed}/{total}',
                    'progress_status': progress_status,
                    'url': f'/hr/hiring/candidates/{cand.id}',
                }
                d['candidate_name'] = cand.full_name or d['candidate_name']
                if cand.phone:
                    d['contact_number'] = cand.phone
            except Exception:
                d['hiring_candidate'] = {
                    'id': cand.id,
                    'full_name': cand.full_name,
                    'role': cand.role or '',
                    'phone': cand.phone or '',
                    'url': f'/hr/hiring/candidates/{cand.id}',
                }
                d['candidate_name'] = cand.full_name or d['candidate_name']
        else:
            d['hiring_candidate'] = None
        if person_of is not None:
            d['person_of'] = person_of
        if person_total is not None:
            d['person_total'] = person_total
            if person_of is not None:
                d['person_label'] = f'{person_of} of {person_total}'
        return d

    def __repr__(self):
        return f'<ManpowerVacancy {self.id} trade={self.trade_id} project={self.project_id}>'


class FilesFolder(db.Model):
    """Folder node in the Files module tree (mirrors Google Drive folders when synced)."""
    __tablename__ = 'files_folders'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey('files_folders.id'), nullable=True, index=True)
    path_key = db.Column(db.String(120), nullable=True, unique=True, index=True)
    drive_folder_id = db.Column(db.String(128), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    parent = db.relationship('FilesFolder', remote_side=[id], backref=db.backref('children', lazy='dynamic'))
    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'parent_id': self.parent_id,
            'path_key': self.path_key,
            'drive_folder_id': self.drive_folder_id,
            'created_by': self.created_by,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f'<FilesFolder {self.id} {self.name}>'


class FilesItem(db.Model):
    """File stored in the Files module (local path + optional Google Drive id)."""
    __tablename__ = 'files_items'

    id = db.Column(db.Integer, primary_key=True)
    folder_id = db.Column(db.Integer, db.ForeignKey('files_folders.id'), nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    mime_type = db.Column(db.String(120), nullable=True)
    size_bytes = db.Column(db.Integer, default=0)
    stored_path = db.Column(db.String(500), nullable=False)
    source_module = db.Column(db.String(40), nullable=True, index=True)  # manpower | leave | upload
    source_kind = db.Column(db.String(40), nullable=True)  # template | export | upload
    sync_status = db.Column(db.String(20), default='local', index=True)  # local | synced | error
    sync_error = db.Column(db.String(500), nullable=True)
    drive_file_id = db.Column(db.String(128), nullable=True)
    last_synced_at = db.Column(db.DateTime, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    folder = db.relationship('FilesFolder', backref=db.backref('items', lazy='dynamic'))
    creator = db.relationship('User', foreign_keys=[created_by])

    def to_dict(self):
        size_b = int(self.size_bytes or 0)
        if size_b >= 1024 * 1024:
            size_label = f'{size_b / (1024 * 1024):.1f} MB'
        elif size_b:
            size_label = f'{max(1, int(round(size_b / 1024)))} KB'
        else:
            size_label = '—'
        return {
            'id': self.id,
            'folder_id': self.folder_id,
            'name': self.name,
            'filename': self.filename,
            'mime_type': self.mime_type or '',
            'size_bytes': size_b,
            'size_label': size_label,
            'source_module': self.source_module or '',
            'source_kind': self.source_kind or '',
            'sync_status': self.sync_status or 'local',
            'sync_error': self.sync_error or '',
            'drive_file_id': self.drive_file_id,
            'last_synced_at': self.last_synced_at.isoformat() if self.last_synced_at else None,
            'created_by': self.created_by,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f'<FilesItem {self.id} {self.filename}>'


class FilesDriveConnection(db.Model):
    """Org-level Google Drive OAuth connection for the Files module (single row)."""
    __tablename__ = 'files_drive_connections'

    id = db.Column(db.Integer, primary_key=True)
    connected_email = db.Column(db.String(255), nullable=True)
    refresh_token_enc = db.Column(db.Text, nullable=True)
    root_drive_folder_id = db.Column(db.String(128), nullable=True)
    connected_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    connected_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    connector = db.relationship('User', foreign_keys=[connected_by])

    def to_dict(self):
        return {
            'connected': bool(self.refresh_token_enc),
            'connected_email': self.connected_email or '',
            'root_drive_folder_id': self.root_drive_folder_id,
            'connected_at': self.connected_at.isoformat() if self.connected_at else None,
            'connected_by': self.connected_by,
        }

    def __repr__(self):
        return f'<FilesDriveConnection email={self.connected_email}>'


class DatabaseBackup(db.Model):
    """Record of an admin-triggered database backup download (file is not stored in the row)."""
    __tablename__ = 'database_backups'

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=_utcnow, index=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    environment = db.Column(db.String(20), nullable=False, default='local')  # local | live
    engine = db.Column(db.String(20), nullable=False, default='sqlite')  # sqlite | postgresql
    filename = db.Column(db.String(255), nullable=False)
    size_bytes = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), nullable=False, default='ok')  # ok | failed
    error_message = db.Column(db.String(500), nullable=True)
    kind = db.Column(db.String(30), nullable=False, default='download')  # download | scheduled

    creator = db.relationship('User', foreign_keys=[created_by_user_id])

    def to_dict(self):
        size_b = int(self.size_bytes or 0)
        if size_b >= 1024 * 1024:
            size_label = f'{size_b / (1024 * 1024):.1f} MB'
        elif size_b:
            size_label = f'{max(1, int(round(size_b / 1024)))} KB'
        else:
            size_label = '—'
        creator = self.creator
        return {
            'id': self.id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'created_by_user_id': self.created_by_user_id,
            'created_by_name': (creator.full_name or creator.username) if creator else '—',
            'environment': self.environment or 'local',
            'engine': self.engine or '',
            'filename': self.filename,
            'size_bytes': size_b,
            'size_label': size_label,
            'status': self.status or 'ok',
            'error_message': self.error_message or '',
            'kind': self.kind or 'download',
        }

    def __repr__(self):
        return f'<DatabaseBackup {self.id} {self.filename} {self.status}>'


class AutomationJob(db.Model):
    """Saved schedule + destination settings for a catalog automation job."""
    __tablename__ = 'automation_jobs'

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), unique=True, nullable=False, index=True)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    schedule_hour = db.Column(db.Integer, default=20, nullable=False)
    schedule_minute = db.Column(db.Integer, default=0, nullable=False)
    timezone = db.Column(db.String(64), default='Asia/Dubai', nullable=False)
    to_emails = db.Column(db.Text, nullable=True)
    save_to_files = db.Column(db.Boolean, default=True, nullable=False)
    send_email = db.Column(db.Boolean, default=True, nullable=False)
    sync_drive = db.Column(db.Boolean, default=True, nullable=False)
    export_modules = db.Column(db.Text, nullable=True)
    last_run_at = db.Column(db.DateTime, nullable=True)
    last_success_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=_utcnow)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow)

    runs = db.relationship(
        'AutomationRun',
        back_populates='job',
        lazy='dynamic',
        cascade='all, delete-orphan',
        order_by='desc(AutomationRun.started_at)',
    )

    def to_dict(self):
        return {
            'id': self.id,
            'slug': self.slug,
            'enabled': bool(self.enabled),
            'schedule_hour': int(self.schedule_hour if self.schedule_hour is not None else 20),
            'schedule_minute': int(self.schedule_minute if self.schedule_minute is not None else 0),
            'timezone': (self.timezone or 'Asia/Dubai').strip() or 'Asia/Dubai',
            'to_emails': (self.to_emails or '').strip(),
            'save_to_files': bool(self.save_to_files),
            'send_email': bool(self.send_email),
            'sync_drive': bool(self.sync_drive),
            'export_modules': (self.export_modules or '').strip(),
            'last_run_at': self.last_run_at.isoformat() if self.last_run_at else None,
            'last_success_at': self.last_success_at.isoformat() if self.last_success_at else None,
            'last_error': self.last_error or '',
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f'<AutomationJob {self.slug} enabled={self.enabled}>'


class AutomationRun(db.Model):
    """One execution of an automation job (manual, cron, or startup catch-up)."""
    __tablename__ = 'automation_runs'

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey('automation_jobs.id'), nullable=False, index=True)
    trigger = db.Column(db.String(20), nullable=False, default='manual')  # manual | scheduler | catchup
    status = db.Column(db.String(20), nullable=False, default='running')  # running | ok | warning | error | skipped
    detail = db.Column(JSON, nullable=True)
    error_message = db.Column(db.String(500), nullable=True)
    started_at = db.Column(db.DateTime, default=_utcnow, index=True)
    finished_at = db.Column(db.DateTime, nullable=True)

    job = db.relationship('AutomationJob', back_populates='runs')

    def to_dict(self):
        return {
            'id': self.id,
            'job_id': self.job_id,
            'slug': self.job.slug if self.job else '',
            'trigger': self.trigger or 'manual',
            'status': self.status or '',
            'detail': self.detail or {},
            'error_message': self.error_message or '',
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'finished_at': self.finished_at.isoformat() if self.finished_at else None,
        }

    def __repr__(self):
        return f'<AutomationRun {self.id} job={self.job_id} {self.status}>'


# Procurement store-keeping tables (must import after db is defined).
from module_procurement.models import (  # noqa: E402
    ProcSupplier, ProcCatalogItem, ProcProperty, ProcStock,
    ProcPurchaseRequest, ProcPurchaseLine, ProcGoodsReceipt,
    ProcGoodsReceiptLine, ProcMovement, ProcPurchaseDocument,
    ProcEmailTemplate,
)

