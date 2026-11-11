//! A minimal XML element tree, parsed and serialized to match Python's
//! `xml.etree.ElementTree` exactly (attribute order preserved, `ET.indent`'s
//! 2-space-per-depth indentation, self-closing tags written as `<tag />`
//! with a space, and the same `&amp;`/`&lt;`/`&gt;`/`&quot;` attribute
//! escaping -- non-ASCII bytes are kept as literal UTF-8, not entity-coded).
//! Every file this engine rewrites (`slice_info.config`,
//! `model_settings.config`) uses only attribute-bearing elements with no
//! mixed text content, so that's all this needs to support.

use quick_xml::events::{BytesStart, Event};
use quick_xml::reader::Reader;

#[derive(Debug, Clone, PartialEq)]
pub struct XmlElement {
    pub tag: String,
    pub attrs: Vec<(String, String)>,
    pub children: Vec<XmlElement>,
}

impl XmlElement {
    pub fn get(&self, key: &str) -> Option<&str> {
        self.attrs.iter().find(|(k, _)| k == key).map(|(_, v)| v.as_str())
    }

    pub fn set(&mut self, key: &str, value: impl Into<String>) {
        if let Some(entry) = self.attrs.iter_mut().find(|(k, _)| k == key) {
            entry.1 = value.into();
        } else {
            self.attrs.push((key.to_string(), value.into()));
        }
    }

    /// Mirrors `Element.find(tag)`: the first direct child with this tag.
    pub fn find_child(&self, tag: &str) -> Option<&XmlElement> {
        self.children.iter().find(|c| c.tag == tag)
    }

    pub fn find_child_mut(&mut self, tag: &str) -> Option<&mut XmlElement> {
        self.children.iter_mut().find(|c| c.tag == tag)
    }

    /// Mirrors `Element.findall(tag)`: all direct children with this tag.
    pub fn find_children(&self, tag: &str) -> impl Iterator<Item = &XmlElement> {
        self.children.iter().filter(move |c| c.tag == tag)
    }

    pub fn find_children_mut<'a>(&'a mut self, tag: &'a str) -> impl Iterator<Item = &'a mut XmlElement> {
        self.children.iter_mut().filter(move |c| c.tag == tag)
    }

    /// Mirrors `plate.remove(filament)` for `filaments[1:]`: keeps only the
    /// first `keep` direct children with this tag, dropping the rest.
    pub fn keep_first_children(&mut self, tag: &str, keep: usize) {
        let mut seen = 0usize;
        self.children.retain(|c| {
            if c.tag != tag {
                return true;
            }
            seen += 1;
            seen <= keep
        });
    }
}

pub fn parse(data: &[u8]) -> Result<XmlElement, String> {
    let mut reader = Reader::from_reader(data);
    let mut stack: Vec<XmlElement> = Vec::new();
    let mut root: Option<XmlElement> = None;

    loop {
        match reader.read_event() {
            Ok(Event::Eof) => break,
            Ok(Event::Start(tag)) => {
                stack.push(element_from_start_tag(&reader, &tag)?);
            }
            Ok(Event::Empty(tag)) => {
                let element = element_from_start_tag(&reader, &tag)?;
                push_completed(&mut stack, &mut root, element);
            }
            Ok(Event::End(_)) => {
                let element = stack.pop().ok_or_else(|| "unbalanced XML end tag".to_string())?;
                push_completed(&mut stack, &mut root, element);
            }
            Ok(_) => continue, // text/comments/decl -- not needed for these files
            Err(e) => return Err(format!("XML parse error: {e}")),
        }
    }
    root.ok_or_else(|| "XML document has no root element".to_string())
}

fn element_from_start_tag(reader: &Reader<&[u8]>, tag: &BytesStart) -> Result<XmlElement, String> {
    let name = String::from_utf8(tag.name().as_ref().to_vec())
        .map_err(|_| "XML tag name is not valid UTF-8".to_string())?;
    let mut attrs = Vec::new();
    for attr in tag.attributes() {
        let attr = attr.map_err(|e| format!("invalid XML attribute: {e}"))?;
        let key = String::from_utf8(attr.key.as_ref().to_vec())
            .map_err(|_| "XML attribute name is not valid UTF-8".to_string())?;
        let value = attr
            .decode_and_unescape_value(reader.decoder())
            .map_err(|e| format!("invalid XML attribute value: {e}"))?
            .into_owned();
        attrs.push((key, value));
    }
    Ok(XmlElement { tag: name, attrs, children: Vec::new() })
}

fn push_completed(stack: &mut Vec<XmlElement>, root: &mut Option<XmlElement>, element: XmlElement) {
    if let Some(parent) = stack.last_mut() {
        parent.children.push(element);
    } else {
        *root = Some(element);
    }
}

/// Mirrors `ET.indent(root, space="  ")` followed by `ET.tostring(root, encoding="utf-8")`.
pub fn serialize_indented(root: &XmlElement) -> Vec<u8> {
    let mut out = Vec::new();
    write_element(&mut out, root, 0);
    out
}

fn write_element(out: &mut Vec<u8>, element: &XmlElement, depth: usize) {
    out.push(b'<');
    out.extend_from_slice(element.tag.as_bytes());
    for (key, value) in &element.attrs {
        out.push(b' ');
        out.extend_from_slice(key.as_bytes());
        out.extend_from_slice(b"=\"");
        escape_attr_value(out, value);
        out.push(b'"');
    }
    if element.children.is_empty() {
        out.extend_from_slice(b" />");
        return;
    }
    out.push(b'>');
    for child in &element.children {
        out.push(b'\n');
        out.extend(std::iter::repeat(b' ').take((depth + 1) * 2));
        write_element(out, child, depth + 1);
    }
    out.push(b'\n');
    out.extend(std::iter::repeat(b' ').take(depth * 2));
    out.extend_from_slice(b"</");
    out.extend_from_slice(element.tag.as_bytes());
    out.push(b'>');
}

fn escape_attr_value(out: &mut Vec<u8>, value: &str) {
    for ch in value.chars() {
        match ch {
            '&' => out.extend_from_slice(b"&amp;"),
            '<' => out.extend_from_slice(b"&lt;"),
            '>' => out.extend_from_slice(b"&gt;"),
            '"' => out.extend_from_slice(b"&quot;"),
            _ => {
                let mut buf = [0u8; 4];
                out.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes());
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_simple_document() {
        let root = parse(b"<config><plate><metadata key=\"a\" value=\"1\"/></plate></config>").unwrap();
        assert_eq!(root.tag, "config");
        let plate = root.find_child("plate").unwrap();
        let metadata = plate.find_child("metadata").unwrap();
        assert_eq!(metadata.get("key"), Some("a"));
        assert_eq!(metadata.get("value"), Some("1"));
    }

    #[test]
    fn serializes_with_indentation_and_space_before_self_close() {
        let root = parse(b"<config><plate><metadata key=\"a\" value=\"1\"/></plate></config>").unwrap();
        let out = serialize_indented(&root);
        assert_eq!(
            out,
            b"<config>\n  <plate>\n    <metadata key=\"a\" value=\"1\" />\n  </plate>\n</config>".to_vec()
        );
    }

    #[test]
    fn escapes_attribute_values() {
        let root = parse(b"<config><item a=\"x&amp;y&lt;z&gt;w&quot;v\"/></config>").unwrap();
        let out = serialize_indented(&root);
        assert_eq!(out, b"<config>\n  <item a=\"x&amp;y&lt;z&gt;w&quot;v\" />\n</config>".to_vec());
    }

    #[test]
    fn keeps_non_ascii_literal() {
        let root = parse("<config><item b=\"caf\u{e9}\"/></config>".as_bytes()).unwrap();
        let out = serialize_indented(&root);
        assert_eq!(out, "<config>\n  <item b=\"caf\u{e9}\" />\n</config>".as_bytes().to_vec());
    }
}
