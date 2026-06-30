<#
Show-RebuildReminder.ps1

Purpose:
  Shows a fullscreen user-facing reminder before scheduled maintenance,
  rebuild, shutdown, or another disruptive action.

Created by:
  Avraham Makovsky

License:
  MIT

Notes:
  - Run inside the logged-in user's interactive session.
  - Session 0 cannot display this UI to the user.
  - The script does not perform maintenance by itself; it only displays a clear
    reminder and waits for user acknowledgement.
#>

param(
    [string]$Title = "Maintenance Reminder",

    [string]$Message = "This machine is scheduled for maintenance. Please save your work and close open applications.",

    [string]$ButtonText = "I understand",

    [string]$NoteText = "Please confirm only after saving your work.",

    [switch]$NoTopMost
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

[System.Windows.Forms.Application]::EnableVisualStyles()

# -----------------------------
# Colors
# -----------------------------
$ColorBackground = [System.Drawing.Color]::FromArgb(18, 32, 54)
$ColorAccent     = [System.Drawing.Color]::FromArgb(97, 233, 255)
$ColorText       = [System.Drawing.Color]::White
$ColorSoftText   = [System.Drawing.Color]::FromArgb(216, 232, 245)
$ColorButtonText = [System.Drawing.Color]::FromArgb(18, 32, 54)

# -----------------------------
# Main form
# -----------------------------
$form = New-Object System.Windows.Forms.Form
$form.Text = $Title
$form.WindowState = [System.Windows.Forms.FormWindowState]::Maximized
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$form.BackColor = $ColorBackground
$form.ForeColor = $ColorText
$form.TopMost = -not $NoTopMost
$form.KeyPreview = $true
$form.AutoScaleMode = [System.Windows.Forms.AutoScaleMode]::Dpi

# -----------------------------
# Main layout
# -----------------------------
$container = New-Object System.Windows.Forms.TableLayoutPanel
$container.Dock = [System.Windows.Forms.DockStyle]::Fill
$container.ColumnCount = 1
$container.RowCount = 5
$container.Padding = New-Object System.Windows.Forms.Padding(60)
$container.BackColor = $ColorBackground

[void]$container.RowStyles.Add(
    (New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent, 18))
)
[void]$container.RowStyles.Add(
    (New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent, 25))
)
[void]$container.RowStyles.Add(
    (New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent, 27))
)
[void]$container.RowStyles.Add(
    (New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent, 20))
)
[void]$container.RowStyles.Add(
    (New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent, 10))
)

# -----------------------------
# Title
# -----------------------------
$titleLabel = New-Object System.Windows.Forms.Label
$titleLabel.Text = $Title
$titleLabel.Dock = [System.Windows.Forms.DockStyle]::Fill
$titleLabel.TextAlign = [System.Drawing.ContentAlignment]::BottomCenter
$titleLabel.Font = New-Object System.Drawing.Font("Segoe UI", 34, [System.Drawing.FontStyle]::Bold)
$titleLabel.ForeColor = $ColorAccent
$titleLabel.BackColor = $ColorBackground

# -----------------------------
# Message
# -----------------------------
$messageLabel = New-Object System.Windows.Forms.Label
$messageLabel.Text = $Message
$messageLabel.Dock = [System.Windows.Forms.DockStyle]::Fill
$messageLabel.TextAlign = [System.Drawing.ContentAlignment]::MiddleCenter
$messageLabel.Font = New-Object System.Drawing.Font("Segoe UI", 24, [System.Drawing.FontStyle]::Regular)
$messageLabel.ForeColor = $ColorText
$messageLabel.BackColor = $ColorBackground
$messageLabel.Padding = New-Object System.Windows.Forms.Padding(30)

# -----------------------------
# Note
# -----------------------------
$noteLabel = New-Object System.Windows.Forms.Label
$noteLabel.Text = $NoteText
$noteLabel.Dock = [System.Windows.Forms.DockStyle]::Fill
$noteLabel.TextAlign = [System.Drawing.ContentAlignment]::TopCenter
$noteLabel.Font = New-Object System.Drawing.Font("Segoe UI", 16, [System.Drawing.FontStyle]::Regular)
$noteLabel.ForeColor = $ColorSoftText
$noteLabel.BackColor = $ColorBackground

# -----------------------------
# Button layout
# This avoids manual Left/Top positioning.
# -----------------------------
$buttonLayout = New-Object System.Windows.Forms.TableLayoutPanel
$buttonLayout.Dock = [System.Windows.Forms.DockStyle]::Fill
$buttonLayout.BackColor = $ColorBackground
$buttonLayout.ColumnCount = 3
$buttonLayout.RowCount = 3

[void]$buttonLayout.ColumnStyles.Add(
    (New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Percent, 50))
)
[void]$buttonLayout.ColumnStyles.Add(
    (New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Absolute, 320))
)
[void]$buttonLayout.ColumnStyles.Add(
    (New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Percent, 50))
)

[void]$buttonLayout.RowStyles.Add(
    (New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent, 50))
)
[void]$buttonLayout.RowStyles.Add(
    (New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Absolute, 82))
)
[void]$buttonLayout.RowStyles.Add(
    (New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent, 50))
)

$button = New-Object System.Windows.Forms.Button
$button.Text = $ButtonText
$button.Dock = [System.Windows.Forms.DockStyle]::Fill
$button.Font = New-Object System.Drawing.Font("Segoe UI", 18, [System.Drawing.FontStyle]::Bold)

# Important:
# Without this, Windows visual styles may ignore BackColor/ForeColor.
$button.UseVisualStyleBackColor = $false

$button.BackColor = $ColorAccent
$button.ForeColor = $ColorButtonText
$button.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
$button.FlatAppearance.BorderSize = 2
$button.FlatAppearance.BorderColor = [System.Drawing.Color]::White
$button.FlatAppearance.MouseOverBackColor = [System.Drawing.Color]::FromArgb(140, 242, 255)
$button.FlatAppearance.MouseDownBackColor = [System.Drawing.Color]::FromArgb(70, 210, 235)
$button.Cursor = [System.Windows.Forms.Cursors]::Hand
$button.Margin = New-Object System.Windows.Forms.Padding(0)

[void]$buttonLayout.Controls.Add($button, 1, 1)

# Optional footer hint
$hintLabel = New-Object System.Windows.Forms.Label
$hintLabel.Text = "Esc is disabled. Use the confirmation button to close this reminder."
$hintLabel.Dock = [System.Windows.Forms.DockStyle]::Fill
$hintLabel.TextAlign = [System.Drawing.ContentAlignment]::TopCenter
$hintLabel.Font = New-Object System.Drawing.Font("Segoe UI", 11, [System.Drawing.FontStyle]::Regular)
$hintLabel.ForeColor = [System.Drawing.Color]::FromArgb(175, 195, 215)
$hintLabel.BackColor = $ColorBackground

# -----------------------------
# Events
# -----------------------------
$button.Add_Click({
    $form.Tag = "confirmed"
    $form.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $form.Close()
})

$form.Add_Shown({
    $form.Activate()
    $button.Focus()
})

$form.Add_FormClosing({
    param($sender, $eventArgs)

    if ($form.Tag -eq "confirmed") {
        return
    }

    $answer = [System.Windows.Forms.MessageBox]::Show(
        $form,
        "Close this reminder without confirming?",
        "Confirm close",
        [System.Windows.Forms.MessageBoxButtons]::YesNo,
        [System.Windows.Forms.MessageBoxIcon]::Question
    )

    if ($answer -ne [System.Windows.Forms.DialogResult]::Yes) {
        $eventArgs.Cancel = $true
    }
})

$form.Add_KeyDown({
    param($sender, $eventArgs)

    if ($eventArgs.KeyCode -eq [System.Windows.Forms.Keys]::Escape) {
        $eventArgs.SuppressKeyPress = $true
    }
})

# -----------------------------
# Compose UI
# -----------------------------
[void]$container.Controls.Add($titleLabel, 0, 0)
[void]$container.Controls.Add($messageLabel, 0, 1)
[void]$container.Controls.Add($noteLabel, 0, 2)
[void]$container.Controls.Add($buttonLayout, 0, 3)
[void]$container.Controls.Add($hintLabel, 0, 4)

[void]$form.Controls.Add($container)

$result = $form.ShowDialog()

if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
    Write-Host "User confirmed the reminder."
    exit 0
}

Write-Host "Reminder window was closed without confirmation."
exit 2
