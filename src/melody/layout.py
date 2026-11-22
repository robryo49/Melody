"""Spacing and dimensions shared by every page so their headers line up."""

# Window edge <-> content, and the gap between two columns of content.
PAGE_MARGIN = 24

# Spacing between the title, the count and the sort dropdown in a header.
HEADER_SPACING = 12

# Width of the header search bar. The Library page reuses it as the width of
# its details-panel artwork, so the search bar sits exactly above the artwork.
SEARCH_WIDTH = 256


def style_header_controls(sort_dropdown, search_entry):
    """Give the sort dropdown and search bar the same size and gap on every page.

    ``sort_dropdown`` is None on a page that has none."""
    search_entry.set_size_request(SEARCH_WIDTH, -1)
    search_entry.set_hexpand(False)

    if sort_dropdown is None:
        # Same gap before the search bar as the dropdown would have left.
        search_entry.set_margin_start(PAGE_MARGIN - HEADER_SPACING)
        return

    # The header box already puts HEADER_SPACING between its children; top it
    # up so the gap between the dropdown and the search bar is PAGE_MARGIN.
    sort_dropdown.set_margin_end(PAGE_MARGIN - HEADER_SPACING)
