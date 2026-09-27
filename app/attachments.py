"""Local-disk storage for journal entry attachments (receipts, invoices,
contracts -- supporting documentation for an audit).

Files are stored under Flask's instance folder (app.instance_path),
which Flask itself keeps outside the application package and which
this project's .gitignore excludes -- exactly the right place for
data that must never end up in the git repo or a delivered zip.
Storage is a plain local folder rather than S3/cloud storage to match
the rest of this app's zero-external-dependency deployment story (see
DEPLOYMENT.md); a self-hosted single-tenant or on-prem deployment
never needs to configure a bucket or credentials for this to work.

Layout: <instance_path>/attachments/<organization_id>/<uuid4>.<ext>
Partitioning by organization_id is defense in depth on top of the
tenant_id check every route below already does at the database level --
even a bug that mixed up which folder to read from would still only
ever expose files within one organization's own subtree, never another
tenant's.
"""
import os
import uuid

from werkzeug.utils import secure_filename

# Deliberately conservative: only formats a legitimate audit document
# would realistically be (receipts, invoices, contracts, scanned
# letters, spreadsheets) -- never anything executable. Rejecting at
# upload time, rather than merely not executing it, means a mislabeled
# or malicious file can't sit in an org's records at all, and can't
# trip an auditor's own antivirus when they download it later.
ALLOWED_EXTENSIONS = {
    "pdf", "jpg", "jpeg", "png", "gif", "webp",
    "doc", "docx", "xls", "xlsx", "csv", "txt",
}
MAX_FILE_SIZE = 15 * 1024 * 1024  # 15MB -- generous for a scanned receipt/contract, not for a bulk dump


class AttachmentError(Exception):
    pass


def _ext(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def validate_upload(file_storage):
    """Raises AttachmentError with a user-facing message if the upload
    isn't acceptable; otherwise returns nothing. Does not trust the
    browser-supplied filename for anything but the extension check --
    the actual stored name is always a fresh UUID (see save_attachment)."""
    if not file_storage or not file_storage.filename:
        raise AttachmentError("Choose a file to attach.")
    ext = _ext(file_storage.filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise AttachmentError(
            f"'.{ext}' files aren't accepted. Allowed types: "
            + ", ".join(sorted(ALLOWED_EXTENSIONS)) + "."
        )
    # Determine size without loading the whole file into memory twice:
    # seek to end, read position, seek back.
    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size == 0:
        raise AttachmentError("That file is empty.")
    if size > MAX_FILE_SIZE:
        raise AttachmentError(f"File is too large ({size / 1024 / 1024:.1f}MB) -- the limit is 15MB.")
    return size


def attachments_root(app, organization_id):
    path = os.path.join(app.instance_path, "attachments", str(organization_id))
    os.makedirs(path, exist_ok=True)
    return path


def save_attachment(app, organization_id, file_storage):
    """Validates and saves file_storage to disk, returning
    (stored_filename, original_filename, content_type, file_size).
    Raises AttachmentError on any validation failure -- callers should
    catch this and flash it rather than let it 500."""
    size = validate_upload(file_storage)
    ext = _ext(file_storage.filename)
    stored_filename = f"{uuid.uuid4().hex}.{ext}"
    original_filename = secure_filename(file_storage.filename) or f"attachment.{ext}"
    dest = os.path.join(attachments_root(app, organization_id), stored_filename)
    file_storage.save(dest)
    return stored_filename, original_filename, file_storage.content_type, size


def attachment_path(app, organization_id, stored_filename):
    return os.path.join(attachments_root(app, organization_id), stored_filename)


def delete_attachment_file(app, organization_id, stored_filename):
    """Best-effort delete -- a missing file on disk (already deleted,
    or never wrote successfully) should never block deleting the DB
    record the user is trying to remove."""
    path = attachment_path(app, organization_id, stored_filename)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
