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

    [switch]$NoTopMost
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

[System.Windows.Forms.Application]::EnableVisualStyles()

$form = New-Object System.Windows.Forms.Form
$form.Text = $Title
$form.WindowState = [System.Windows.Forms.FormWindowState]::Maximized
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$form.BackColor = [System.Drawing.Color]::FromArgb(18, 32, 54)
$form.ForeColor = [System.Drawing.Color]::White
$form.TopMost = -not $NoTopMost
$form.KeyPreview = $true

$container = New-Object System.Windows.Forms.TableLayoutPanel
$container.Dock = [System.Windows.Forms.DockStyle]::Fill
$container.ColumnCount = 1
$container.RowCount = 4
$container.Padding = New-Object System.Windows.Forms.Padding(50)
$container.BackColor = $form.BackColor

$container.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent, 20)))
$container.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent, 25)))
$container.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent, 35)))
$container.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent, 20)))

$titleLabel = New-Object System.Windows.Forms.Label
$titleLabel.Text = $Title
$titleLabel.Dock = [System.Windows.Forms.DockStyle]::Fill
$titleLabel.TextAlign = [System.Drawing.ContentAlignment]::BottomCenter
$titleLabel.Font = New-Object System.Drawing.Font("Segoe UI", 34, [System.Drawing.FontStyle]::Bold)
$titleLabel.ForeColor = [System.Drawing.Color]::FromArgb(97, 233, 255)

$messageLabel = New-Object System.Windows.Forms.Label
$messageLabel.Text = $Message
$messageLabel.Dock = [System.Windows.Forms.DockStyle]::Fill
$messageLabel.TextAlign = [System.Drawing.ContentAlignment]::MiddleCenter
$messageLabel.Font = New-Object System.Drawing.Font("Segoe UI", 24, [System.Drawing.FontStyle]::Regular)
$messageLabel.ForeColor = [System.Drawing.Color]::White
$messageLabel.Padding = New-Object System.Windows.Forms.Padding(30)

$noteLabel = New-Object System.Windows.Forms.Label
$noteLabel.Text = "Please confirm only after saving your work."
$noteLabel.Dock = [System.Windows.Forms.DockStyle]::Fill
$noteLabel.TextAlign = [System.Drawing.ContentAlignment]::TopCenter
$noteLabel.Font = New-Object System.Drawing.Font("Segoe UI", 16, [System.Drawing.FontStyle]::Regular)
$noteLabel.ForeColor = [System.Drawing.Color]::FromArgb(216, 232, 245)

$buttonPanel = New-Object System.Windows.Forms.Panel
$buttonPanel.Dock = [System.Windows.Forms.DockStyle]::Fill

$button = New-Object System.Windows.Forms.Button
$button.Text = $ButtonText
$button.Width = 260
$button.Height = 70
$button.Font = New-Object System.Drawing.Font("Segoe UI", 18, [System.Drawing.FontStyle]::Bold)
$button.BackColor = [System.Drawing.Color]::FromArgb(97, 233, 255)
$button.ForeColor = [System.Drawing.Color]::FromArgb(18, 32, 54)
$button.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
$button.FlatAppearance.BorderSize = 0

$buttonPanel.Add($button)

# Keep the confirmation button centered when screen size changes.
$buttonPanel.Add_Resize({
    $button.Left = [int](($buttonPanel.Width - $button.Width) / 2)
    $button.Top = 10
})

$button.Add_Click({
    $form.Tag = "confirmed"
    $form.Close()
})

$form.Add_FormClosing({
    param($sender, $eventArgs)

    if ($form.Tag -eq "confirmed") {
        return
    }

    $answer = [System.Windows.Forms.MessageBox]::Show(
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

$container.Controls.Add($titleLabel, 0, 0)
$container.Controls.Add($messageLabel, 0, 1)
$container.Controls.Add($noteLabel, 0, 2)
$container.Controls.Add($buttonPanel, 0, 3)

$form.Controls.Add($container)

[void]$form.ShowDialog()
