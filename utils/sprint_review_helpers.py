import pandas as pd
import requests
import os
import io
import re
import base64
import streamlit as st
from html import unescape
from datetime import datetime
from urllib.parse import quote
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether, Image as ReportLabImage, Flowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas
from reportlab.graphics.shapes import Drawing, Line, PolyLine
from PIL import Image as PILImage
from utils.kpi_helpers import sprint_kpi_summary_df
from utils.pdf_helpers import (
    SmartKeepTogether, hex_to_reportlab_color, convert_markdown_to_pdf_rich_text,
    split_bugs_and_topics, sort_items_by_label_priority, get_team_label, get_custom_label, sort_items_by_type_and_epic,
    draw_background_landscape, NumberedCanvas, build_demos_pdf_block, build_next_releases_pdf_block,
    format_status_with_emoji, build_custom_extra_table_pdf_block, get_arrow_drawing, get_jira_link_paragraph,
    extract_numeric_version
)


def resolve_custom_bug_column(config):
    """Return (enabled, name, labels) for the custom bug column. Enabled whenever labels are non-empty."""
    cfg = (config or {}).get("custom_bug_column") or {}
    labels = [str(l).strip() for l in (cfg.get("labels") or []) if str(l).strip()]
    name = str(cfg.get("name") or "").strip() or "Custom"
    return (len(labels) > 0, name, labels)


def get_overview_custom_bug_value(item_type, labels, target_labels):
    """Display value for the Overview custom column: matching label for Bugs, '' otherwise."""
    if str(item_type).strip().lower() != "bug":
        return ""
    if not target_labels:
        return ""
    if labels is None or (not isinstance(labels, (list, tuple, set)) and pd.isna(labels)):
        return ""
    if isinstance(labels, (list, tuple, set)):
        labels = " ".join(str(l) for l in labels)
    val = get_custom_label(labels, target_labels)
    return "" if val == "-" else val


def add_custom_bug_display_column(df, name, labels):
    """Return a copy of df with a derived column between Type and Labels (display only).
    Returns an unchanged copy on blank name, empty labels, name collision or missing Type/Labels."""
    out = df.copy()
    name = str(name or "").strip()
    if not name or not labels or name in out.columns or "Type" not in out.columns:
        return out
    values = [
        get_overview_custom_bug_value(t, l, labels)
        for t, l in zip(out["Type"], out["Labels"] if "Labels" in out.columns else [""] * len(out))
    ]
    out.insert(list(out.columns).index("Type") + 1, name, values)
    return out


def build_bugs_table_data(bugs_df, custom_col, styles):
    """Build (rows, col_widths) for the Bugs / Planned Bugs PDF tables.
    custom_col: None or (name, labels). styles: header, header_center, body, body_bold, body_center."""
    has_custom_col = custom_col is not None
    header_row = [
        Paragraph("Epic", styles["header"]),
        Paragraph("Key", styles["header"]),
        Paragraph("Summary", styles["header"]),
        Paragraph("Status", styles["header_center"]),
        Paragraph("Fix Version", styles["header"]),
        Paragraph("Team", styles["header"]),
    ]
    if has_custom_col:
        header_row.insert(5, Paragraph(custom_col[0], styles["header"]))
    data = [header_row]
    sorted_bugs = sort_items_by_label_priority(bugs_df, ["Epic", "Key"])
    last_epic = None
    for _, row in sorted_bugs.iterrows():
        epic_val = str(row['Epic']).strip() if pd.notna(row['Epic']) else "-"
        if epic_val in ["", "No Epic", "nan"]:
            epic_val = "-"
        orig_fv = str(row['Fix Version']).strip() if pd.notna(row['Fix Version']) else ""
        fv_val = extract_numeric_version(row['Fix Version'])
        if not fv_val:
            if "not release relevant" in orig_fv.lower() or orig_fv.upper() == "N.R.R.":
                fv_val = "N.R.R."
            else:
                fv_val = "-"
        display_epic = epic_val
        if display_epic == last_epic:
            if display_epic == "-":
                epic_cell = Paragraph("-", styles["body"])
            else:
                epic_cell = get_arrow_drawing(colors.HexColor("#64748B"))
        else:
            last_epic = display_epic
            epic_cell = Paragraph(display_epic, styles["body"])
        row_data = [
            epic_cell,
            get_jira_link_paragraph(row['Key'], styles["body_bold"]),
            Paragraph(str(row['Summary']), styles["body"]),
            Paragraph(format_status_with_emoji(row['Status']), styles["body_center"]),
            Paragraph(fv_val, styles["body"]),
            Paragraph(get_team_label(row.get('Labels', '')), styles["body"]),
        ]
        if has_custom_col:
            row_data.insert(5, Paragraph(get_custom_label(row.get('Labels', ''), custom_col[1]), styles["body"]))
        data.append(row_data)
    if has_custom_col:
        col_widths = [130, 75, 205, 50, 59, 70, 95]
    else:
        col_widths = [130, 75, 275, 50, 59, 95]
    return data, col_widths


def build_sprint_review_pdf(overview_df, outlook_df, config):
    pdf_buffer = io.BytesIO()
    
    # Setup document geometry for landscape presentation slide format
    doc = SimpleDocTemplate(
        pdf_buffer,
        pagesize=landscape(letter),
        leftMargin=54,
        rightMargin=54,
        topMargin=54,
        bottomMargin=54
    )
    
    styles = getSampleStyleSheet()
    primary_color_hex = config.get('primary_color')
    primary_color = hex_to_reportlab_color(primary_color_hex)
    
    # Custom styles (optimized for large, presentation-grade text size)
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=32,
        leading=36,
        textColor=primary_color,
        spaceAfter=12
    )
    
    subtitle_style = ParagraphStyle(
        'DocSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=13.5,
        leading=18,
        textColor=colors.HexColor("#475569"),
        spaceAfter=25
    )
    
    section_title_style = ParagraphStyle(
        'SecTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=18,
        leading=22,
        textColor=primary_color,
        spaceBefore=15,
        spaceAfter=8
    )
    
    cell_header_style = ParagraphStyle(
        'CellHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.5,
        leading=11,
        textColor=colors.white
    )
    
    cell_body_style = ParagraphStyle(
        'CellBody',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.0,
        leading=10.5,
        textColor=colors.HexColor("#1E293B")
    )
    
    cell_body_bold_style = ParagraphStyle(
        'CellBodyBold',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.0,
        leading=10.5,
        textColor=colors.HexColor("#1E293B")
    )
    
    sub_section_title_style = ParagraphStyle(
        'SubSecTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=14.5,
        leading=18.5,
        textColor=colors.HexColor("#475569"),
        spaceBefore=12,
        spaceAfter=5
    )

    cell_header_center_style = ParagraphStyle(
        'CellHeaderCenter',
        parent=cell_header_style,
        alignment=1 # Center
    )
    
    cell_body_center_style = ParagraphStyle(
        'CellBodyCenter',
        parent=cell_body_style,
        alignment=1 # Center
    )
    
    legend_style = ParagraphStyle(
        'LegendStyle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor("#64748B"),
        alignment=2, # Right
        spaceBefore=2,
        spaceAfter=10
    )

    # Cover Page Styles (optimized for landscape presentation)
    cover_project_style = ParagraphStyle(
        'CoverProject',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=16,
        textColor=colors.HexColor("#64748B"),
        alignment=1, # Center
        spaceAfter=8
    )
    
    cover_title_style = ParagraphStyle(
        'CoverTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=40,
        leading=34,
        textColor=primary_color,
        alignment=1, # Center
        spaceAfter=6
    )
    
    cover_subtitle_style = ParagraphStyle(
        'CoverSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=24,
        leading=20,
        textColor=colors.HexColor("#334155"),
        alignment=1, # Center
        spaceAfter=15
    )
    
    cover_date_style = ParagraphStyle(
        'CoverDate',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#64748B"),
        alignment=1, # Center
        spaceAfter=15
    )
    
    cover_welcome_style = ParagraphStyle(
        'CoverWelcome',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=15,
        textColor=colors.HexColor("#475569"),
        alignment=1, # Center
        spaceBefore=15,
        spaceAfter=15
    )

    story = []

    # --- STARTING COVER PAGE ---
    sprint_num = "Sprint 14"
    if 'sprint_number' in st.session_state and str(config.get('sprint_number')).strip() != "":
        sprint_num = str(config.get('sprint_number')).strip()
    elif 'ov_sprint_num' in st.session_state and str(config.get('ov_sprint_num')).strip() != "":
        sprint_num = f"Sprint {config.get('ov_sprint_num')}"
        
    from datetime import datetime
    current_date = datetime.now().strftime("%d-%m-%Y")
    
    story.append(Spacer(1, 15))
    story.append(Paragraph(config.get('project_name').upper(), cover_project_style))
    story.append(Paragraph("Sprint Review", cover_title_style))
    story.append(Spacer(1, 15))
    story.append(Paragraph(f"Sprint: {sprint_num}", cover_subtitle_style))
    story.append(Paragraph(f"Date: {current_date}", cover_date_style))
    story.append(Spacer(1, 5))
    
    cover_image_path = config.get('sr_cover_temp_path')
    if cover_image_path and os.path.exists(cover_image_path):
        try:
            pil_img = PILImage.open(cover_image_path)
            orig_w, orig_h = pil_img.size
            max_w = 320  # Optimized landscape cover width
            max_h = 160  # Optimized landscape cover height
            scale = min(max_w / orig_w, max_h / orig_h)
            img = ReportLabImage(cover_image_path, width=orig_w * scale, height=orig_h * scale)
            img.hAlign = 'CENTER'
            story.append(img)
            story.append(Spacer(1, 10))
        except Exception:
            pass
            
    welcome_text = config.get('sprint_welcome_message')
    if welcome_text:
        story.append(Paragraph(welcome_text, cover_welcome_style))
        
    story.append(PageBreak())
    # --- END OF COVER PAGE ---
    
    # 2. Section 1: Overview
    topics_ov, bugs_ov = split_bugs_and_topics(overview_df)

    if not topics_ov.empty or not bugs_ov.empty:
        prefix_flowables = [
            Paragraph("Overview:", section_title_style),
            Paragraph('<font color="#22C55E">●</font> Done &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <font color="#F59E0B">●</font> In Progress &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <font color="#EF4444">●</font> Blocked &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <font color="#3B82F6">●</font> To Do', legend_style)
        ]
        
        # 2a. Delivered Topics Sub-section
        if not topics_ov.empty:
            topics_flowables = []
            if prefix_flowables:
                topics_flowables.extend(prefix_flowables)
                prefix_flowables = []
            topics_flowables.append(Paragraph("Worked Topics", sub_section_title_style))
            # Col Widths: Total = 684pt (Landscape)
            table_data = [[
                Paragraph("Epic", cell_header_style),
                Paragraph("Key", cell_header_style),
                Paragraph("Summary", cell_header_style),
                Paragraph("Status", cell_header_center_style),
                Paragraph("Fix Version", cell_header_style),
                Paragraph("Team", cell_header_style)
            ]]
            
            sorted_topics = sort_items_by_type_and_epic(topics_ov)
            
            last_epic = None
            for _, row in sorted_topics.iterrows():
                epic_val = str(row['Epic']).strip() if pd.notna(row['Epic']) else "-"
                if epic_val in ["", "No Epic", "nan"]:
                    epic_val = "-"
                orig_fv = str(row['Fix Version']).strip() if pd.notna(row['Fix Version']) else ""
                fv_val = extract_numeric_version(row['Fix Version'])
                if not fv_val:
                    if "not release relevant" in orig_fv.lower() or orig_fv.upper() == "N.R.R.":
                        fv_val = "N.R.R."
                    else:
                        fv_val = "-"
                    
                display_epic = epic_val
                if display_epic == last_epic:
                    if display_epic == "-":
                        epic_cell = Paragraph("-", cell_body_style)
                    else:
                        epic_cell = get_arrow_drawing(colors.HexColor("#64748B"))
                else:
                    last_epic = display_epic
                    epic_cell = Paragraph(display_epic, cell_body_style)
                    
                table_data.append([
                    epic_cell,
                    get_jira_link_paragraph(row['Key'], cell_body_bold_style),
                    Paragraph(str(row['Summary']), cell_body_style),
                    Paragraph(format_status_with_emoji(row['Status']), cell_body_center_style),
                    Paragraph(fv_val, cell_body_style),
                    Paragraph(get_team_label(row.get('Labels', '')), cell_body_style)
                ])
                
            topics_table = Table(
                table_data,
                colWidths=[110, 75, 295, 50, 59, 95]
            )
            topics_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), primary_color),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('ALIGN', (3, 0), (3, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('LEFTPADDING', (0, 0), (-1, -1), 8),
                ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                ('GRID', (0, 0), (-1, -1), 0.8, colors.HexColor("#E2E8F0")),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
            ]))
            topics_flowables.append(topics_table)
            topics_flowables.append(Spacer(1, 10))
            story.append(SmartKeepTogether(topics_flowables))
            
        # 2b. Resolved Bugs Sub-section
        if not bugs_ov.empty:
            bugs_flowables = []
            if prefix_flowables:
                bugs_flowables.extend(prefix_flowables)
                prefix_flowables = []
            bugs_flowables.append(Paragraph("Bugs", sub_section_title_style))
            has_custom_col, custom_col_name, custom_col_labels = resolve_custom_bug_column(config)
            custom_col = (custom_col_name, custom_col_labels) if has_custom_col else None
            bug_data, col_widths = build_bugs_table_data(
                bugs_ov, custom_col,
                {
                    "header": cell_header_style, "header_center": cell_header_center_style,
                    "body": cell_body_style, "body_bold": cell_body_bold_style,
                    "body_center": cell_body_center_style,
                }
            )
            bugs_table = Table(
                bug_data,
                colWidths=col_widths
            )
            bugs_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), primary_color),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('ALIGN', (3, 0), (3, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('LEFTPADDING', (0, 0), (-1, -1), 8),
                ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                ('GRID', (0, 0), (-1, -1), 0.8, colors.HexColor("#E2E8F0")),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
            ]))
            bugs_flowables.append(bugs_table)
            bugs_flowables.append(Spacer(1, 10))
            story.append(SmartKeepTogether(bugs_flowables))
        
    story.append(Spacer(1, 10))
    
    # Render custom extra tables set to "Before Demo Table"
    before_tables = [t for t in config.get('custom_tables') if t.get("position") == "Before Demo Table"]
    for t in before_tables:
        df_ext = t["df"]
        if df_ext is not None and not df_ext.empty:
            df_render = df_ext.drop(columns=["Select"]) if "Select" in df_ext.columns else df_ext.copy()
            if t.get("sort_by_team"):
                df_render = sort_items_by_label_priority(df_render, ["Key"])
                if "Labels" in df_render.columns:
                    df_render["Team"] = df_render["Labels"].apply(get_team_label)
                    df_render = df_render[[column for column in df_render.columns if column != "Team"] + ["Team"]]
            hidden_cols = t.get("hidden_cols", [])
            df_render = df_render.drop(columns=[col for col in hidden_cols if col in df_render.columns])
            story.append(PageBreak())
            extra_title = t["title"] if t["title"].strip() != "" else "Special Metrics Overview"
            extra_blocks = build_custom_extra_table_pdf_block(df_render, primary_color, styles, is_landscape=True)
            if extra_blocks:
                story.append(SmartKeepTogether([
                    Paragraph(extra_title, section_title_style),
                    Spacer(1, 10)
                ] + extra_blocks))

    # 2c. Product Demos Presenters (Moved before Outlook!)
    if overview_df is not None and not overview_df.empty:
        demo_blocks = build_demos_pdf_block(overview_df, primary_color, styles, sub_section_style=sub_section_title_style, is_landscape=True)
        if demo_blocks:
            story.extend(demo_blocks)
            story.append(Spacer(1, 15))

    # Render custom extra tables set to "After Demo Table"
    after_tables = [t for t in config.get('custom_tables') if t.get("position") == "After Demo Table"]
    for t in after_tables:
        df_ext = t["df"]
        if df_ext is not None and not df_ext.empty:
            df_render = df_ext.drop(columns=["Select"]) if "Select" in df_ext.columns else df_ext.copy()
            if t.get("sort_by_team"):
                df_render = sort_items_by_label_priority(df_render, ["Key"])
                if "Labels" in df_render.columns:
                    df_render["Team"] = df_render["Labels"].apply(get_team_label)
                    df_render = df_render[[column for column in df_render.columns if column != "Team"] + ["Team"]]
            hidden_cols = t.get("hidden_cols", [])
            df_render = df_render.drop(columns=[col for col in hidden_cols if col in df_render.columns])
            story.append(PageBreak())
            extra_title = t["title"] if t["title"].strip() != "" else "Special Metrics Overview"
            extra_blocks = build_custom_extra_table_pdf_block(df_render, primary_color, styles, is_landscape=True)
            if extra_blocks:
                story.append(SmartKeepTogether([
                    Paragraph(extra_title, section_title_style),
                    Spacer(1, 10)
                ] + extra_blocks))
            
    # 2d. Sprint KPI Summary (optional, rendered right before the Outlook section)
    if config.get("include_sprint_kpis", False):
        selected_kpis = config.get("sprint_kpi_selection") or []
        metrics = config.get("current_sprint_metrics") or {}
        kpi_df = sprint_kpi_summary_df(selected_kpis, metrics)
        if not kpi_df.empty:
            kpi_blocks = build_custom_extra_table_pdf_block(kpi_df, primary_color, styles, is_landscape=True)
            if kpi_blocks:
                story.append(PageBreak())
                story.append(SmartKeepTogether([
                    Paragraph("Sprint KPI Summary:", section_title_style),
                    Spacer(1, 10)
                ] + kpi_blocks))
                story.append(Spacer(1, 12))

    # 3. Section 2: Outlook (Page Break isolation)
    topics_ot, bugs_ot = split_bugs_and_topics(outlook_df)
    next_release_df = config.get('next_release_df')
    
    has_outlook = (not topics_ot.empty) or (not bugs_ot.empty) or (next_release_df is not None and not next_release_df.empty)
    if has_outlook:
        story.append(PageBreak())
        prefix_flowables = [
            Paragraph("Outlook:", section_title_style),
            Paragraph('<font color="#22C55E">●</font> Done &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <font color="#F59E0B">●</font> In Progress &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <font color="#EF4444">●</font> Blocked &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; <font color="#3B82F6">●</font> To Do', legend_style)
        ]
        
        # 3a. Planned Topics Sub-section
        if not topics_ot.empty:
            outlook_flowables = []
            if prefix_flowables:
                outlook_flowables.extend(prefix_flowables)
                prefix_flowables = []
            outlook_flowables.append(Paragraph("Planned Topics", sub_section_title_style))
            table_data_outlook = [[
                Paragraph("Epic", cell_header_style),
                Paragraph("Key", cell_header_style),
                Paragraph("Summary", cell_header_style),
                Paragraph("Status", cell_header_center_style),
                Paragraph("Fix Version", cell_header_style),
                Paragraph("Team", cell_header_style)
            ]]
            
            sorted_outlook = sort_items_by_type_and_epic(topics_ot)
            
            last_epic = None
            for _, row in sorted_outlook.iterrows():
                epic_val = str(row['Epic']).strip() if pd.notna(row['Epic']) else "-"
                if epic_val in ["", "No Epic", "nan"]:
                    epic_val = "-"
                orig_fv = str(row['Fix Version']).strip() if pd.notna(row['Fix Version']) else ""
                fv_val = extract_numeric_version(row['Fix Version'])
                if not fv_val:
                    if "not release relevant" in orig_fv.lower() or orig_fv.upper() == "N.R.R.":
                        fv_val = "N.R.R."
                    else:
                        fv_val = "-"
                    
                display_epic = epic_val
                if display_epic == last_epic:
                    if display_epic == "-":
                        epic_cell = Paragraph("-", cell_body_style)
                    else:
                        epic_cell = get_arrow_drawing(colors.HexColor("#64748B"))
                else:
                    last_epic = display_epic
                    epic_cell = Paragraph(display_epic, cell_body_style)
                    
                table_data_outlook.append([
                    epic_cell,
                    get_jira_link_paragraph(row['Key'], cell_body_bold_style),
                    Paragraph(str(row['Summary']), cell_body_style),
                    Paragraph(format_status_with_emoji(row['Status']), cell_body_center_style),
                    Paragraph(fv_val, cell_body_style),
                    Paragraph(get_team_label(row.get('Labels', '')), cell_body_style)
                ])
                
            outlook_table = Table(
                table_data_outlook,
                colWidths=[110, 75, 295, 50, 59, 95]
            )
            outlook_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), primary_color),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('ALIGN', (3, 0), (3, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('LEFTPADDING', (0, 0), (-1, -1), 8),
                ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                ('GRID', (0, 0), (-1, -1), 0.8, colors.HexColor("#E2E8F0")),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
            ]))
            outlook_flowables.append(outlook_table)
            outlook_flowables.append(Spacer(1, 10))
            story.append(SmartKeepTogether(outlook_flowables))
            
        # 3b. Planned Bugs Sub-section
        if not bugs_ot.empty:
            bugs_outlook_flowables = []
            if prefix_flowables:
                bugs_outlook_flowables.extend(prefix_flowables)
                prefix_flowables = []
            bugs_outlook_flowables.append(Paragraph("Planned Bugs", sub_section_title_style))
            has_custom_col, custom_col_name, custom_col_labels = resolve_custom_bug_column(config)
            custom_col = (custom_col_name, custom_col_labels) if has_custom_col else None
            bug_data_outlook, col_widths = build_bugs_table_data(
                bugs_ot, custom_col,
                {
                    "header": cell_header_style, "header_center": cell_header_center_style,
                    "body": cell_body_style, "body_bold": cell_body_bold_style,
                    "body_center": cell_body_center_style,
                }
            )
            bugs_outlook_table = Table(
                bug_data_outlook,
                colWidths=col_widths
            )
            bugs_outlook_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), primary_color),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('ALIGN', (3, 0), (3, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('LEFTPADDING', (0, 0), (-1, -1), 8),
                ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                ('GRID', (0, 0), (-1, -1), 0.8, colors.HexColor("#E2E8F0")),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
            ]))
            bugs_outlook_flowables.append(bugs_outlook_table)
            bugs_outlook_flowables.append(Spacer(1, 10))
            story.append(SmartKeepTogether(bugs_outlook_flowables))
     
        # 3c. Target Releases Sub-section (Moved inside Outlook!)
        if next_release_df is not None and not next_release_df.empty:
            target_release_flowables = []
            if prefix_flowables:
                target_release_flowables.extend(prefix_flowables)
                prefix_flowables = []
            target_release_flowables.append(Paragraph("Target Releases", sub_section_title_style))
            rel_blocks = build_next_releases_pdf_block(next_release_df, primary_color, styles, is_landscape=True)
            if rel_blocks:
                story.append(SmartKeepTogether(target_release_flowables + rel_blocks))
            
    doc.build(story, canvasmaker=NumberedCanvas, onFirstPage=draw_background_landscape, onLaterPages=draw_background_landscape)
    pdf_buffer.seek(0)
    return pdf_buffer


# ---------------------------------------------------------


